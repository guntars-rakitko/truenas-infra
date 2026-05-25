"""mc tool — wraps minio-client CLI for MinIO + B2.

Aliases (`nas-prd`, `nas-dev`, `b2-eu`) are configured at container start
(via setup script in a future task). Tests just verify argv shaping +
JSON-line parsing.
"""
from cluster_agent.tools.mc import mc_ls


def test_mc_ls_calls_correct_alias(monkeypatch):
    """mc_ls('nas-prd/mssql-backups/') invokes `mc ls --json nas-prd/mssql-backups/`."""
    called = {}

    def fake_run(cmd, **kw):
        called["cmd"] = cmd

        class R:
            returncode = 0
            stdout = '{"key":"foo","size":100}'
            stderr = ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    result = mc_ls("nas-prd/mssql-backups/")
    assert called["cmd"][0] == "mc"
    assert "ls" in called["cmd"]
    assert "--json" in called["cmd"]
    assert "nas-prd/mssql-backups/" in called["cmd"]
    assert isinstance(result, list)
    assert result[0]["key"] == "foo"
