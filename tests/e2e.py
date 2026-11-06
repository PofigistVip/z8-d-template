"""Exercise the template using a temporary PostgreSQL instance and HTTP."""
from pathlib import Path
import argparse
import http.client
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

project = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--z8-d", type=Path, default=os.environ.get("Z8_D_ROOT"), required=not os.environ.get("Z8_D_ROOT"))
parser.add_argument("--postgres-bin", type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.z8_d.resolve() / "tests"))
from postgres_support import postgres_bin, temporary_postgres

bin_dir = postgres_bin(args.postgres_bin)
exe = project / "build" / ("z8-template.exe" if os.name == "nt" else "z8-template")
public = project / "build/public"
generated = project / "build/generated"
document_module = generated / "z8gen/org/zenframework/z8/template_/model/Document.d"
if not exe.is_file() or not (public / "index.html").is_file() or not document_module.is_file():
    raise SystemExit("Build the template before running the end-to-end test")
assert "class Z8_Document" in document_module.read_text(encoding="utf-8")
assert "import z8lib." in document_module.read_text(encoding="utf-8")
assert not (generated / "z8gen/org/zenframework/z8/server").exists(), "Framework classes were generated in the application"
assert not list(generated.glob(".build-*")), "Temporary build files were not removed"
psql = bin_dir / ("psql.exe" if os.name == "nt" else "psql")
documents = "org.zenframework.z8.template.view.Documents"
mine = "org.zenframework.z8.template.view.MyDocuments"
with tempfile.TemporaryDirectory() as directory, temporary_postgres(bin_dir) as (connection, env):
    temp = Path(directory)
    config = temp / "server.properties"
    settings = "web.server.http.host=127.0.0.1\nweb.server.http.port=19080\nweb.client.hashPassword=false\nscheduler.enabled=false\n"
    office_home = os.environ.get("Z8_TEST_OFFICE_HOME")
    if office_home:
        settings += "office.home=" + Path(office_home).resolve().as_posix() + "\n"
    config.write_text(settings, encoding="utf-8")
    command = [str(exe), connection, "public", "--initialize", "--config", str(config)]
    for _ in range(2):
        result = subprocess.run(command, env=env, cwd=temp, capture_output=True, text=True, encoding="utf-8", check=True)
        assert result.stdout.strip() == "initialized", result.stdout
    invalid_config = temp / "invalid.properties"
    for invalid in ("0", "65536", "not-a-port"):
        invalid_config.write_text("web.server.http.port=" + invalid + "\n", encoding="ascii")
        result = subprocess.run([str(exe), connection, "public", "--initialize", "--config", str(invalid_config)],
                                env=env, cwd=temp, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode and "web.server.http.port" in result.stderr, result.stderr
    subprocess.run([str(psql), "-v", "ON_ERROR_STOP=1", "-c",
                    '''UPDATE "SystemUsers" SET "Password"=md5('template-test') WHERE "RecordId"='00000000-0000-0000-0000-000000000002' ''', connection],
                   env=env, capture_output=True, check=True)

    def sql(query):
        return subprocess.check_output([str(psql), "-tAc", query, connection], env=env, text=True).strip()

    assert sql('SELECT count(*) FROM "Tmp Statuses" WHERE "RecordId" <> \'00000000-0000-0000-0000-000000000000\'') == "3"
    server = subprocess.Popen([str(exe), connection, "public", "--public-dir", str(public)],
                              env=env, cwd=temp, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
    try:
        line = server.stdout.readline().strip()
        if not line.isdigit():
            raise RuntimeError("Server did not start: " + line + server.stderr.read())
        port = int(line)
        assert port == 19080, port

        def http_request(path, method="GET", body=None, headers=None, include_headers=False):
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                client.request(method, path, body, headers or {})
                response = client.getresponse()
                content = response.read()
                return (response.status, dict(response.getheaders()), content) if include_headers else (response.status, content)
            finally:
                client.close()

        def request(**parameters):
            status, body = http_request("/request.json?" + urlencode(parameters))
            result = json.loads(body)
            assert status == 200 and result.get("success"), (status, result)
            return result

        status, index = http_request("/index.html")
        assert status == 200 and b"z8-template.js" in index
        status, script = http_request("/z8-template.js")
        assert status == 200 and b"org.zenframework.z8.template.controls.EMail" in script
        for source in (project / "src/main/js").rglob("*.js"):
            assert source.read_bytes() in script, source
        status, debug_index = http_request("/debug.html")
        assert status == 200 and b"debug/z8-template.js" in debug_index
        status, debug_script = http_request("/debug/z8-template.js")
        assert status == 200 and debug_script == script
        status, debug_css = http_request("/debug/css/z8-template.css")
        assert status == 200 and debug_css
        login = request(request="login", login="Admin", password="template-test")
        assert {documents, mine} <= {entry["request"] for entry in login["user"]["entries"]}
        session = login["session"]
        metadata = request(request=documents, session=session, action="meta")
        assert [item["name"] for item in metadata["controls"]] == ["createdAt", "name", "doc", "status.name", "user.name", "email", "historyList"]
        assert any(item["name"] == "clearHistory" for item in metadata["actions"])
        created = request(request=documents, session=session, action="create", fields='["name","email"]',
                          data='[{"name":"First","email":"a@example.com"}]')
        record = created["data"][0]["recordId"]
        # The original form includes the empty FileField array when opening a
        # linked-field selector for the newly created document.
        selector_values = {"recordId": record, "doc": [],
                           "statusId": "00000000-0000-0000-0000-000000000000",
                           "userId": "00000000-0000-0000-0000-000000000000"}
        choices = request(request=documents, session=session, action="read", query="status",
                          fields='["status.name"]', values=json.dumps(selector_values))
        assert len(choices["data"]) >= 3, choices
        request(request=documents, session=session, action="create", fields='["name"]', data=json.dumps([{"name": "Другой <&>"}]))
        request(request=documents, session=session, action="update", fields='["name","statusId","userId"]',
                data=json.dumps([{"recordId": record, "statusId": "BA82DA79-E026-4DE2-AA41-7C5E0A821320",
                                  "status.name": "Новый", "user.name": "Admin",
                                  "userId": "00000000-0000-0000-0000-000000000002"}]))
        assert sql('SELECT encode(convert_to("Name", \'UTF8\'), \'hex\') FROM "Tmp Statuses" WHERE "RecordId"=\'BA82DA79-E026-4DE2-AA41-7C5E0A821320\'') == "Новый".encode().hex()
        history = '''SELECT count(*) FROM "Tmp History" WHERE "Document" = '%s' ''' % record
        assert sql(history) == "1"
        # Listbox emits a value-only dependency followed by its parent-link
        # filter when status.name refreshes the history control.
        history_rows = request(request=documents, session=session, action="read", query="history",
                               fields='["history.createdAt","history.status.name","history.user.name","history.description","history.documentId"]',
                               where=json.dumps([{"value": "BA82DA79-E026-4DE2-AA41-7C5E0A821320"},
                                                 {"property": "history.documentId", "value": record}]))
        assert len(history_rows["data"]) == 1, history_rows
        history_new = request(request=documents, session=session, action="create", query="history",
                              fields='["history.createdAt","history.status.name","history.user.name","history.description","history.documentId"]',
                              data=json.dumps([{"history.recordId": "00000000-0000-0000-0000-000000000000",
                                                "history.documentId": record,
                                                "undefined": "00000000-0000-0000-0000-000000000002"}]))
        assert len(history_new["data"]) == 1, history_new
        assert sql(history) == "2"
        history_id = history_new["data"][0]["history.recordId"]
        edited = request(request=documents, session=session, action="update", query="history",
                         fields='["history.description","history.documentId"]',
                         data=json.dumps([{"history.recordId": history_id, "history.description": "Note"}]))
        assert edited["data"][0]["history.description"] == "Note", edited
        deleted = request(request=documents, session=session, action="destroy", query="history",
                          data=json.dumps([{"history.recordId": history_id}]))
        assert deleted["success"] and sql(history) == "1", deleted
        history_new = request(request=documents, session=session, action="create", query="history",
                              fields='["history.createdAt","history.status.name","history.user.name","history.description","history.documentId"]',
                              data=json.dumps([{"history.recordId": "00000000-0000-0000-0000-000000000000",
                                                "history.documentId": record,
                                                "history.description": "Note"}]))
        assert len(history_new["data"]) == 1 and sql(history) == "2", history_new
        reread = request(request=documents, session=session, action="read", recordId=record,
                         data=json.dumps([{"recordId": record}]))
        assert len(reread["data"]) == 1 and reread["data"][0]["recordId"] == record, reread
        copied = request(request=documents, session=session, action="copy", recordId=json.dumps([record]),
                         fields='["name","statusId","userId"]',
                         data='[{"recordId":"00000000-0000-0000-0000-000000000000"}]')["data"][0]
        assert copied["recordId"] != record and copied["name"] == "First", copied
        assert copied["statusId"] == "BA82DA79-E026-4DE2-AA41-7C5E0A821320" and copied["userId"] == "00000000-0000-0000-0000-000000000002", copied
        request(request=documents, session=session, action="destroy", data=json.dumps([copied["recordId"]]))
        rows = request(request=mine, session=session, action="read", fields='["name","statusId","userId"]')["data"]
        assert [row["recordId"] for row in rows] == [record], rows

        def export(session, view, **extra):
            result = request(request=view, session=session, action="export", format="xls",
                             columns=json.dumps([{"id": "name", "width": 150, "minWidth": 50}]), **extra)
            file = result["info"]["files"][0]
            assert file["name"].endswith(".xlsx"), file
            status, content = http_request("/" + quote(file["path"], safe="/") + "?" + urlencode({"session": session}))
            assert status == 200, (status, content)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            column = root.find(".//s:col", ns)
            assert column is not None and column.get("customWidth") == "1" and 28 < float(column.get("width")) < 29, ET.tostring(root)
            return [[text.text or "" for text in row.findall(".//s:t", ns)]
                    for row in root.findall(".//s:row", ns)]

        exported = export(session, documents, where=json.dumps({"property": "name", "value": "First"}))
        assert len(exported) == 2 and exported[1] == ["First"], exported
        if office_home:
            generated_file = request(request=documents, session=session, action="export", format="xls",
                                columns='[{"id":"name"}]')["info"]["files"][0]
            generated_preview = "/" + quote(generated_file["path"], safe="/") + "?" + urlencode({"session": session, "preview": "true"})
            status, headers, content = http_request(generated_preview, include_headers=True)
            assert status == 200 and headers.get("Content-Type") == "application/pdf" and content.startswith(b"%PDF-"), (status, headers)
        exported = export(session, documents)
        assert {tuple(row) for row in exported[1:]} == {("First",), ("Другой <&>",)}, exported
        nested = request(request=documents, session=session, action="export", format="xls", query="history",
                         columns=json.dumps([{"id": field, "width": 150, "minWidth": 50}
                                             for field in ("history.createdAt", "history.status.name", "history.user.name")]),
                         where=json.dumps([{"value": "BA82DA79-E026-4DE2-AA41-7C5E0A821320"},
                                           {"property": "history.documentId", "value": record}]),
                         values=json.dumps(selector_values))
        assert nested["info"]["files"][0]["name"] == "export.xlsx", nested
        status, nested_content = http_request("/" + quote(nested["info"]["files"][0]["path"], safe="/") + "?" + urlencode({"session": session}))
        assert status == 200, status
        with zipfile.ZipFile(io.BytesIO(nested_content)) as archive:
            nested_sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        assert len([item for item in nested_sheet.iter() if item.tag.endswith("}row")]) == 3
        pdf = request(request=documents, session=session, action="export", format="pdf",
                      columns='[{"id":"name","width":150,"minWidth":50}]')["info"]["files"][0]
        assert pdf["name"] == "export.pdf", pdf
        status, pdf_bytes = http_request("/" + quote(pdf["path"], safe="/") + "?" + urlencode({"session": session}))
        assert status == 200 and pdf_bytes.startswith(b"%PDF-1.4"), (status, pdf_bytes[:100])
        preview_url = "/" + quote(pdf["path"], safe="/") + "?" + urlencode({"session": session, "preview": "false"})
        status, headers, preview = http_request(preview_url, include_headers=True)
        assert status == 200 and headers.get("Content-Type") == "application/pdf" and "Content-Disposition" not in headers and preview == pdf_bytes, headers
        stamped_url = "/" + quote(pdf["path"], safe="/") + "?" + urlencode({"session": session, "preview": "true", "stamps": "[]"})
        status, headers, preview = http_request(stamped_url, include_headers=True)
        assert status == 200 and headers.get("Content-Type") == "application/pdf" and preview == pdf_bytes, headers
        assert b"/MediaBox [0 0 842 595]" in pdf_bytes, "Default PDF page should be landscape A4"
        assert (public / "css/fonts/roboto/roboto-regular.ttf").read_bytes() in pdf_bytes
        weighted = request(request=documents, session=session, action="export", format="pdf",
                           columns=json.dumps([{"id": "name", "width": 300}, {"id": "email", "width": 100, "minWidth": 300}]))["info"]["files"][0]
        status, weighted_pdf = http_request("/" + quote(weighted["path"], safe="/") + "?" + urlencode({"session": session}))
        rectangles = re.findall(rb"q (\d+) (\d+) (\d+) 18 re W n", weighted_pdf)
        assert status == 200 and len(rectangles) >= 2, rectangles
        assert [(int(x), int(width)) for x, _, width in rectangles[:2]] == [(28, 486), (514, 300)], rectangles[:2]
        constrained = request(request=documents, session=session, action="export", format="pdf",
                              columns=json.dumps([{"id": "name", "width": 0, "minWidth": 900}, {"id": "email", "width": 0, "minWidth": 900}]))["info"]["files"][0]
        status, constrained_pdf = http_request("/" + quote(constrained["path"], safe="/") + "?" + urlencode({"session": session}))
        rectangles = re.findall(rb"q (\d+) (\d+) (\d+) 18 re W n", constrained_pdf)
        assert status == 200 and [(int(x), int(width)) for x, _, width in rectangles[:2]] == [(28, 393), (421, 393)], rectangles[:2]
        status, body = http_request("/request.json?" + urlencode({"request": documents, "session": session, "action": "export", "format": "pdf",
                                    "columns": json.dumps([{"id": "name", "width": -1}])}))
        assert not json.loads(body).get("success"), body
        portrait = request(request=documents, session=session, action="export", format="pdf",
                           columns='[{"id":"name"}]', options=json.dumps({"pageFormat": "A5", "pageOrientation": "portrait", "leftMargin": 5}))["info"]["files"][0]
        status, portrait_bytes = http_request("/" + quote(portrait["path"], safe="/") + "?" + urlencode({"session": session}))
        assert status == 200 and b"/MediaBox [0 0 420 595]" in portrait_bytes and b"q 14 " in portrait_bytes, status
        bottom = request(request=documents, session=session, action="export", format="pdf", columns='[{"id":"name"}]',
                         options=json.dumps({"detailRowHeight": "10mm", "detailVertAlign": "bottom"}))["info"]["files"][0]
        status, bottom_pdf = http_request("/" + quote(bottom["path"], safe="/") + "?" + urlencode({"session": session}))
        positions = re.findall(rb"q (\d+) (\d+) \d+ 28 re W n BT /F1 9 Tf \d+ (\d+) Td", bottom_pdf)
        assert status == 200 and positions and all(int(baseline) - int(y) == 3 for _, y, baseline in positions), positions
        for options in ({"pageOrientation": "diagonal"}, {"pageFormat": "unknown"}, {"leftMargin": 9999},
                        {"detailVertAlign": "sideways"}, {"detailRowHeight": "5bogus"}):
            status, body = http_request("/request.json?" + urlencode({"request": documents, "session": session, "action": "export",
                                        "format": "pdf", "columns": '[{"id":"name"}]', "options": json.dumps(options)}))
            assert not json.loads(body).get("success"), body
        def pdf_text(content):
            mapping = {int(cid, 16): bytes.fromhex(unicode.decode()).decode("utf-16-be")
                       for cid, unicode in re.findall(rb"<([0-9A-F]{4})> <([0-9A-F]{4,8})>\n", content)}
            return "".join("".join(mapping[int(chars[i:i+4], 16)] for i in range(0, len(chars), 4))
                           for chars in re.findall(rb"<([0-9A-F]+)> Tj", content))
        text = pdf_text(pdf_bytes)
        assert "First" in text and "Другой <&>" in text, text
        if os.environ.get("Z8_TEST_PDF"):
            Path(os.environ["Z8_TEST_PDF"]).write_bytes(pdf_bytes)
        for word_format in ("doc", "docx"):
            word_filter = {} if word_format == "doc" else {"where": json.dumps({"property": "name", "value": "First"})}
            word = request(request=documents, session=session, action="export", format=word_format,
                           columns='[{"id":"name"}]', **word_filter)["info"]["files"][0]
            assert word["name"] == "export.docx", word
            status, content = http_request("/" + quote(word["path"], safe="/") + "?" + urlencode({"session": session}))
            assert status == 200, status
            if word_format == "doc" and os.environ.get("Z8_TEST_DOCX"):
                Path(os.environ["Z8_TEST_DOCX"]).write_bytes(content)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                doc = ET.fromstring(archive.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            rows = [[text.text or "" for text in row.findall(".//w:t", ns)] for row in doc.findall(".//w:tr", ns)]
            expected = {("First",), ("Другой <&>",)} if word_format == "doc" else {("First",)}
            assert {tuple(row) for row in rows[1:]} == expected, rows

        html_file = request(request=documents, session=session, action="export", format="html",
                            columns='[{"id":"missing"},{"id":"name"}]')["info"]["files"][0]
        assert html_file["name"] == "export.html", html_file
        status, content = http_request("/" + quote(html_file["path"], safe="/") + "?" + urlencode({"session": session}))
        html = content.decode("utf-8")
        assert status == 200 and "<td>First</td>" in html, html
        assert "<td>Другой &lt;&amp;&gt;</td>" in html, html
        assert "<td>Другой <&></td>" not in html, html

        # The original schema generator grants the User role access to
        # MyDocuments but denies access to the administrator's Documents view.
        users = "org.zenframework.z8.server.base.table.system.view.UsersView"
        password_hash = hashlib.md5(b"user-test").hexdigest()
        account = request(request=users, session=session, action="create", fields='["name"]',
                          data=json.dumps([{"name": "TemplateUser", "password": password_hash}]))
        user_id = account["data"][0]["recordId"]
        request(request=users, session=session, action="create", query="form.userRoles",
                fields='["form.userRoles.user","form.userRoles.role"]',
                data=json.dumps([{"form.userRoles.user": user_id, "form.userRoles.role": "421A8413-A8EC-4DAB-9235-9A5FF83341C5"}]),
                values=json.dumps({"recordId": user_id}))
        user_login = request(request="login", login="TemplateUser", password="user-test")
        user_session = user_login["session"]
        status, _ = http_request("/" + quote(pdf["path"], safe="/") + "?" + urlencode({"session": user_session}))
        assert status != 200, "A different session downloaded the administrator's PDF"
        other_rows = request(request=mine, session=user_session, action="read", fields='["name"]')["data"]
        assert not other_rows, other_rows
        user_meta = request(request=mine, session=user_session, action="meta")
        assert any(item["name"] == "status.name" for item in user_meta["controls"]), user_meta
        own = request(request=mine, session=user_session, action="create", fields='["name"]',
                      data='[{"name":"Own document"}]')["data"][0]["recordId"]
        request(request=mine, session=user_session, action="update", fields='["name","userId"]',
                data=json.dumps([{"recordId": own, "userId": user_id, "user.name": "TemplateUser"}]))
        assert [row["recordId"] for row in request(request=mine, session=user_session, action="read", fields='["name"]')["data"]] == [own]
        exported = export(user_session, mine)
        assert len(exported) == 2 and exported[1] == ["Own document"], exported
        status, body = http_request("/request.json?" + urlencode({"request": documents, "session": user_session, "action": "export", "format": "xls", "columns": '[{"id":"name"}]'}))
        assert not json.loads(body).get("success"), body
        status, body = http_request("/request.json?" + urlencode({"request": documents, "session": user_session, "action": "export", "format": "doc", "columns": '[{"id":"name"}]'}))
        assert not json.loads(body).get("success"), body
        status, body = http_request("/request.json?" + urlencode({"request": documents, "session": user_session, "action": "export", "format": "html", "columns": '[{"id":"name"}]'}))
        assert not json.loads(body).get("success"), body
        status, body = http_request("/request.json?" + urlencode({"request": documents, "session": user_session, "action": "meta"}))
        denied = json.loads(body)
        assert not denied.get("success"), (status, denied)

        status, body = http_request("/request.json?" + urlencode({"request": "login", "session": user_session,
                                    "password": "wrong", "newPassword": "new-user-test"}))
        assert not json.loads(body).get("success"), body
        request(request="login", session=user_session, password="user-test", newPassword="new-user-test")
        status, body = http_request("/request.json?" + urlencode({"request": "login", "login": "TemplateUser", "password": "user-test"}))
        assert status != 200 or not json.loads(body).get("success"), body
        relogin = request(request="login", login="TemplateUser", password="new-user-test")
        assert relogin["user"]["login"] == "TemplateUser", relogin
        renewed_session = relogin["session"]
        temporary_file = request(request=mine, session=renewed_session, action="export", format="doc",
                                 columns='[{"id":"name"}]')["info"]["files"][0]
        request(request="logout", session=renewed_session)
        status, _ = http_request("/request.json?" + urlencode({"request": mine, "session": renewed_session, "action": "read"}))
        assert status != 200, "Logged-out session was still authorized"
        status, _ = http_request("/" + quote(temporary_file["path"], safe="/") + "?" + urlencode({"session": renewed_session}))
        assert status != 200, "Logged-out session downloaded a generated file"

        params = {"request": documents, "session": session, "action": "attach", "field": "doc",
                  "recordId": record, "fields": '["name","doc"]'}
        boundary = "z8-template-test"
        body = b"".join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                        for key, value in params.items())
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="document.txt"\r\n'
                 'Content-Type: text/plain\r\n\r\nHello template\r\n').encode() + f'--{boundary}--\r\n'.encode()
        status, body = http_request("/request.json", "POST", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        attached = json.loads(body)
        assert status == 200 and attached["success"], attached
        file = json.loads(attached["data"][0]["doc"])[0]
        status, contents = http_request("/" + quote(file["path"], safe="/") + "?" + urlencode({"session": session, "id": file["id"]}))
        assert status == 200 and contents == b"Hello template", (status, contents)
        if office_home:
            preview_url = "/" + quote(file["path"], safe="/") + "?" + urlencode({"session": session, "id": file["id"], "preview": "true"})
            status, headers, preview = http_request(preview_url, include_headers=True)
            assert status == 200 and headers.get("Content-Type") == "application/pdf" and preview.startswith(b"%PDF-"), (status, headers)
        for format in ("xls", "pdf", "doc", "html"):
            exported = request(request=documents, session=session, action="export", format=format,
                               columns='[{"id":"name"},{"id":"createdAt"},{"id":"doc"}]',
                               where=json.dumps({"property": "name", "value": "First"}))["info"]["files"][0]
            status, content = http_request("/" + quote(exported["path"], safe="/") + "?" + urlencode({"session": session}))
            assert status == 200, status
            if format == "xls":
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                texts = " ".join(item.text or "" for item in root.iter() if item.tag.endswith("}t"))
            elif format == "pdf":
                texts = pdf_text(content)
            elif format == "doc":
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    root = ET.fromstring(archive.read("word/document.xml"))
                texts = " ".join(item.text or "" for item in root.iter() if item.tag.endswith("}t"))
            else:
                texts = content.decode("utf-8")
            assert "document.txt" in texts and re.search(r"\d{2}\.\d{2}\.\d{4}", texts), texts
        detached = request(request=documents, session=session, action="detach", field="doc", recordId=record,
                           fields='["name","doc"]', data=json.dumps([file["id"]]))
        assert json.loads(detached["data"][0]["doc"]) == [], detached
        status, contents = http_request("/" + quote(file["path"], safe="/") + "?" + urlencode({"session": session, "id": file["id"]}))
        assert status != 200, (status, contents)
        if office_home:
            email_body = b"".join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                                  for key, value in params.items())
            email_body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="message.eml"\r\n'
                           'Content-Type: message/rfc822\r\n\r\n'
                           'From: sender@example.com\r\nSubject: Template email\r\n'
                           'Content-Type: text/plain; charset=UTF-8\r\n\r\nHello from email\r\n').encode()
            email_body += f'\r\n--{boundary}--\r\n'.encode()
            status, body = http_request("/request.json", "POST", email_body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
            attached_email = json.loads(body)
            assert status == 200 and attached_email["success"], attached_email
            email_file = json.loads(attached_email["data"][0]["doc"])[0]
            email_url = "/" + quote(email_file["path"], safe="/") + "?" + urlencode({"session": session, "id": email_file["id"], "preview": "true"})
            status, headers, preview = http_request(email_url, include_headers=True)
            assert status == 200 and headers.get("Content-Type") == "application/pdf" and preview.startswith(b"%PDF-"), (status, headers)
            request(request=documents, session=session, action="detach", field="doc", recordId=record,
                    fields='["name","doc"]', data=json.dumps([email_file["id"]]))
        pdf_body = b"".join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                            for key, value in params.items())
        pdf_body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="document.pdf"\r\n'
                     'Content-Type: application/pdf\r\n\r\n').encode() + pdf_bytes + f'\r\n--{boundary}--\r\n'.encode()
        status, body = http_request("/request.json", "POST", pdf_body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        attached_pdf = json.loads(body)
        assert status == 200 and attached_pdf["success"], attached_pdf
        pdf_attachment = json.loads(attached_pdf["data"][0]["doc"])[0]
        preview_url = "/" + quote(pdf_attachment["path"], safe="/") + "?" + urlencode({"session": session, "id": pdf_attachment["id"], "preview": "false"})
        status, headers, preview = http_request(preview_url, include_headers=True)
        assert status == 200 and headers.get("Content-Type") == "application/pdf" and "Content-Disposition" not in headers and preview == pdf_bytes, headers
        request(request=documents, session=session, action="detach", field="doc", recordId=record,
                fields='["name","doc"]', data=json.dumps([pdf_attachment["id"]]))
        status, _ = http_request(preview_url)
        assert status != 200, status
        request(request=documents, session=session, action="action", name="clearHistory",
                records=json.dumps([record]), selection="[]", parameters="[]")
        assert sql(history) == "0"
        request(request=documents, session=session, action="create", fields='["name"]',
                data=json.dumps([{"name": "Page test %02d" % i} for i in range(45)]))
        multipage = request(request=documents, session=session, action="export", format="pdf",
                            columns='[{"id":"name"}]')["info"]["files"][0]
        status, content = http_request("/" + quote(multipage["path"], safe="/") + "?" + urlencode({"session": session}))
        assert status == 200 and b"/Count 2 /Kids" in content, (status, content[:100])
        tall = request(request=documents, session=session, action="export", format="pdf", columns='[{"id":"name"}]',
                       options=json.dumps({"detailRowHeight": "36pt", "detailVertAlign": "top"}))["info"]["files"][0]
        status, tall_pdf = http_request("/" + quote(tall["path"], safe="/") + "?" + urlencode({"session": session}))
        assert status == 200 and b"/Count 4 /Kids" in tall_pdf, (status, tall_pdf[:100])
        positions = re.findall(rb"q (\d+) (\d+) \d+ 36 re W n BT /F1 9 Tf \d+ (\d+) Td", tall_pdf)
        assert positions and all(int(baseline) - int(y) == 24 for _, y, baseline in positions), positions
        assert len(export(session, documents)) == 49  # 48 records plus the header
    finally:
        if server.poll() is None:
            server.stdin.write("\n")
            server.stdin.flush()
            server.wait(timeout=15)
        if server.returncode:
            raise RuntimeError("Server failed: " + server.stderr.read())
print("Template build, initialization, login, documents, history, filter and files passed")
