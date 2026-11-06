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
    config.write_text("web.server.http.host=127.0.0.1\nweb.server.http.port=19080\nweb.client.hashPassword=false\nscheduler.enabled=false\n", encoding="ascii")
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

        def http_request(path, method="GET", body=None, headers=None):
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                client.request(method, path, body, headers or {})
                response = client.getresponse()
                return response.status, response.read()
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
            return [[text.text or "" for text in row.findall(".//s:t", ns)]
                    for row in root.findall(".//s:row", ns)]

        exported = export(session, documents, where=json.dumps({"property": "name", "value": "First"}))
        assert len(exported) == 2 and exported[1] == ["First"], exported
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
        assert (public / "css/fonts/roboto/roboto-regular.ttf").read_bytes() in pdf_bytes
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
        status, body = http_request("/request.json?" + urlencode({"request": documents, "session": user_session, "action": "meta"}))
        denied = json.loads(body)
        assert not denied.get("success"), (status, denied)

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
        for format in ("xls", "pdf"):
            exported = request(request=documents, session=session, action="export", format=format,
                               columns='[{"id":"name"},{"id":"createdAt"},{"id":"doc"}]',
                               where=json.dumps({"property": "name", "value": "First"}))["info"]["files"][0]
            status, content = http_request("/" + quote(exported["path"], safe="/") + "?" + urlencode({"session": session}))
            assert status == 200, status
            if format == "xls":
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                texts = " ".join(item.text or "" for item in root.iter() if item.tag.endswith("}t"))
            else:
                texts = pdf_text(content)
            assert "document.txt" in texts and re.search(r"\d{2}\.\d{2}\.\d{4}", texts), texts
        request(request=documents, session=session, action="action", name="clearHistory",
                records=json.dumps([record]), selection="[]", parameters="[]")
        assert sql(history) == "0"
        request(request=documents, session=session, action="create", fields='["name"]',
                data=json.dumps([{"name": "Page test %02d" % i} for i in range(45)]))
        multipage = request(request=documents, session=session, action="export", format="pdf",
                            columns='[{"id":"name"}]')["info"]["files"][0]
        status, content = http_request("/" + quote(multipage["path"], safe="/") + "?" + urlencode({"session": session}))
        assert status == 200 and b"/Count 2 /Kids" in content, (status, content[:100])
        assert len(export(session, documents)) == 49  # 48 records plus the header
    finally:
        if server.poll() is None:
            server.stdin.write("\n")
            server.stdin.flush()
            server.wait(timeout=15)
        if server.returncode:
            raise RuntimeError("Server failed: " + server.stderr.read())
print("Template build, initialization, login, documents, history, filter and files passed")
