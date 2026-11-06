import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import project_manager as pm


class SafetyTests(unittest.TestCase):
    def test_delete_undo_and_guards(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            base = Path(tmp); core = base/'core.db'; drafts = base/'drafts.db'
            with sqlite3.connect(core, factory=pm.Connection) as c:
                c.executescript('CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT, name TEXT, sandboxes TEXT); CREATE TABLE session_v2(id TEXT PRIMARY KEY, project_id TEXT REFERENCES project(id) ON DELETE CASCADE);')
                c.executemany('INSERT INTO project VALUES(?,?,NULL,?)', [('empty','C:/empty','[]'), ('active','C:/active','[]'),('history','C:/history','[]')])
                c.execute("INSERT INTO session_v2 VALUES('s','history')")
            with sqlite3.connect(drafts, factory=pm.Connection) as c:
                c.execute('CREATE TABLE state(name TEXT,key TEXT,value TEXT)')
                c.execute('INSERT INTO state VALUES(?,?,?)', ('opencode.global.dat','server',json.dumps({'usedProjectIDs':{'local':['active']},'lastProjectID':{'local':'active'},'projects':{}})))
            with pm.connect(core) as c: digest = pm.schema_digest(c)
            with patch.object(pm, 'DRAFTS', drafts), patch.object(pm,'stopped'), patch.object(pm, 'SUPPORTED_SCHEMA', digest):
                before = core.read_bytes()
                for pid in ('active','history'):
                    with self.assertRaises(ValueError): pm.delete_empty(pid,core,base/'backups')
                self.assertEqual(before,core.read_bytes())
                with patch.object(pm, 'SUPPORTED_SCHEMA', 'unknown'):
                    with self.assertRaisesRegex(ValueError, '数据库结构'): pm.delete_empty('empty',core,base/'backups')
                real_connect = pm.connect
                calls = 0
                def failing_backup(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 3: raise OSError('backup unavailable')
                    return real_connect(*args, **kwargs)
                with patch.object(pm, 'connect', side_effect=failing_backup):
                    with self.assertRaises(OSError): pm.delete_empty('empty',core,base/'backups')
                with pm.connect(core) as c:
                    self.assertIsNotNone(c.execute("SELECT id FROM project WHERE id='empty'").fetchone())
                folder = pm.delete_empty('empty',core,base/'backups')
                with sqlite3.connect(core, factory=pm.Connection) as c:
                    self.assertEqual(c.execute('SELECT id FROM project ORDER BY id').fetchall(), [('active',),('history',)])
                    self.assertEqual(c.execute('SELECT count(*) FROM session_v2').fetchone()[0],1)
                with sqlite3.connect(folder/'opencode.db', factory=pm.Connection) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM project').fetchone()[0],3)
                pm.undo(folder/'undo.json',core)
                with sqlite3.connect(core, factory=pm.Connection) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM project').fetchone()[0],3)
                with self.assertRaises(sqlite3.IntegrityError): pm.undo(folder/'undo.json',core)
                manifest = json.loads((folder/'undo.json').read_text(encoding='utf-8'))
                manifest['project']['worktree'] = 'C:/tampered'
                (folder/'undo.json').write_text(json.dumps(manifest),encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '不一致'): pm.undo(folder/'undo.json',core)

    def test_running_process_refuses(self):
        class Result:
            stdout = b'"OpenCode.exe","123","Console","1","123 K"\r\n'
        with patch.object(pm.subprocess,'run',return_value=Result()):
            with self.assertRaises(ValueError): pm.stopped()


if __name__ == '__main__': unittest.main()
