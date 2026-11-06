"""Exercise the template using a temporary PostgreSQL instance and HTTP."""
from pathlib import Path
import argparse
import http.client
import json
import os
import subprocess
import sys
import tempfile
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
if not exe.is_file() or not (public / "index.html").is_file():
    raise SystemExit("Build the template before running the end-to-end test")
psql = bin_dir / ("psql.exe" if os.name == "nt" else "psql")
documents = "org.zenframework.z8.template.view.Documents"
mine = "org.zenframework.z8.template.view.MyDocuments"
with tempfile.TemporaryDirectory() as directory, temporary_postgres(bin_dir) as (connection, env):
    temp = Path(directory)
    config = temp / "server.properties"
    config.write_text("web.client.hashPassword=false\nscheduler.enabled=false\n", encoding="ascii")
    command = [str(exe), connection, "public", "--initialize", "--config", str(config)]
    for _ in range(2):
        result = subprocess.run(command, env=env, cwd=temp, capture_output=True, text=True, encoding="utf-8", check=True)
        assert result.stdout.strip() == "initialized", result.stdout
    subprocess.run([str(psql), "-v", "ON_ERROR_STOP=1", "-c",
                    '''UPDATE "SystemUsers" SET "Password"=md5('template-test') WHERE "RecordId"='00000000-0000-0000-0000-000000000002' ''', connection],
                   env=env, capture_output=True, check=True)

    def sql(query):
        return subprocess.check_output([str(psql), "-tAc", query, connection], env=env, text=True).strip()

    assert sql('SELECT count(*) FROM "Tmp Statuses" WHERE "RecordId" <> \'00000000-0000-0000-0000-000000000000\'') == "3"
    server = subprocess.Popen([str(exe), connection, "public", "--listen", "127.0.0.1:0", "--public-dir", str(public), "--config", str(config)],
                              env=env, cwd=temp, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
    try:
        line = server.stdout.readline().strip()
        if not line.isdigit():
            raise RuntimeError("Server did not start: " + line + server.stderr.read())
        port = int(line)

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
        request(request=documents, session=session, action="create", fields='["name"]', data='[{"name":"Other"}]')
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
        rows = request(request=mine, session=session, action="read", fields='["name","statusId","userId"]')["data"]
        assert [row["recordId"] for row in rows] == [record], rows

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
        request(request=documents, session=session, action="action", name="clearHistory",
                records=json.dumps([record]), selection="[]", parameters="[]")
        assert sql(history) == "0"
    finally:
        if server.poll() is None:
            server.stdin.write("\n")
            server.stdin.flush()
            server.wait(timeout=15)
        if server.returncode:
            raise RuntimeError("Server failed: " + server.stderr.read())
print("Template build, initialization, login, documents, history, filter and files passed")
