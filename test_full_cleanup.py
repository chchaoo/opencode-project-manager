import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import full_cleanup as f
import project_manager as pm


class FullRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name); self.core=self.base/'core.db'; self.desktop=self.base/'desktop.db'
        self.directory=self.base/'project-files'; self.directory.mkdir()
        (self.directory/'keep.txt').write_bytes(b'user project content\x00\xff')
        for folder in ('.git','.opencode','nested'):
            (self.directory/folder).mkdir()
            (self.directory/folder/'keep').write_bytes(b'original repository and config data')
        for path, fixture in [(self.core,'core_schema.json'),(self.desktop,'desktop_schema.json')]:
            with sqlite3.connect(path, factory=pm.Connection) as c:
                for sql in json.loads((Path(__file__).parent/'tests'/fixture).read_text(encoding='utf-8')): c.execute(sql)
        self.pid='project-a'; self.other='project-b'; self.sid='ses_a'; self.sb='ses_b'
        self.path=str(self.directory).replace('\\','/')
        with pm.connect(self.core,True) as c:
            self.insert(c,'project',id=self.pid,worktree=self.path,sandboxes='[]')
            self.insert(c,'project',id=self.other,worktree='C:/other',sandboxes='[]')
            self.insert(c,'project',id='global',worktree='/',sandboxes='[]')
            for sid,pid,path in [(self.sid,self.pid,self.path),('ses_child',self.pid,self.path),(self.sb,self.other,'C:/other')]:
                self.insert(c,'session_v2',id=sid,project_id=pid,directory=path,workspace_id='workspace-shared',parent_id=self.sid if sid=='ses_child' else None)
                self.insert(c,'session_message',id='msg_'+sid,session_id=sid,data='{}')
                self.insert(c,'session_part',message_id='msg_'+sid,session_id=sid,ordinal=0,data='{}')
                self.insert(c,'session_inbox',id='inbox_'+sid,session_id=sid,payload='{}')
                self.insert(c,'instruction_entry',session_id=sid,key='instruction',value='text')
                self.insert(c,'instruction_state',session_id=sid,initial_values='{}',current_values='{}')
                self.insert(c,'event_sequence',aggregate_id=sid,owner_id=pid)
                self.insert(c,'event',id='event_'+sid,aggregate_id=sid,data='{}')
                self.insert(c,'schedule',id='schedule_'+sid,session_id=sid)
                self.insert(c,'job',notification_id='job_'+sid,session_id=sid)
            self.insert(c,'workspace',id='workspace-shared')
            for table in ('permission','worktree','project_directory'):
                for pid in (self.pid,self.other):
                    values={'project_id':pid}
                    if table=='permission':values['id']='perm_'+pid
                    else:values['directory']=self.path if pid==self.pid else 'C:/other'
                    self.insert(c,table,**values)
            for h in ('unique','shared'):
                self.insert(c,'blob',hash=h,data=b'BLOB-'+h.encode())
                self.insert(c,'blob_ref',session_id=self.sid,message_id='msg_'+self.sid,hash=h)
            self.insert(c,'blob_ref',session_id=self.sb,message_id='msg_'+self.sb,hash='shared')
            self.insert(c,'credential',id='unrelated',value='KEEP SECRET')
        self.name=f.storage_name('workspace',self.path)
        self.draft_id='draft_target'; self.draft_name=f.storage_name('draft',self.draft_id)
        route='/server/bG9jYWw/session/'+self.sid
        with pm.connect(self.desktop,True) as c:
            self.state(c,'opencode.global.dat','server',{'list':[],'projects':{},'usedProjectIDs':{'local':[self.pid,self.other],'remote':[self.pid]},'lastProjectID':{'local':self.pid,'remote':self.pid}})
            self.state(c,'opencode.window.test.dat','tabs',[
                {'type':'session','server':'local','sessionId':self.sid},
                {'type':'session','server':'local','sessionId':self.sb},
                {'type':'session','server':'remote','sessionId':self.sid},
                {'type':'draft','server':'local','draftID':self.draft_id,'directory':self.path}])
            self.state(c,'opencode.window.test.dat','tabs.closed',[{'tab':{'type':'session','server':'local','sessionId':self.sid},'index':0}])
            self.state(c,'opencode.window.test.dat','tabs.info',{'local\n'+route:{'directory':self.path},'keep':{'title':'other'}})
            self.state(c,'opencode.window.test.dat','tabs.recent',{'key':'local\n'+route})
            self.state(c,self.name,'session:'+self.sid+':layout',{'text':'local state'})
            self.state(c,self.draft_name,'draft:layout',{'text':'draft state'})
            self.state(c,'opencode.global.dat','model',{'keep':'model settings'})
            c.execute('INSERT INTO document VALUES(?,?)',(self.name+':session:'+self.sid+':prompt',json.dumps({'blob':'draft-blob'})))
            c.execute('INSERT INTO blob VALUES(?,?,?)',('draft-blob',b'private draft',0))
        self.stop=patch.object(pm,'stopped');self.stop.start();self.addCleanup(self.stop.stop)

    def insert(self,c,table,**values):
        record={}
        for col in c.execute('PRAGMA table_info('+pm.quote(table)+')'):
            name,kind,required=col[1],col[2],col[3]
            record[name]=values.get(name, (0 if 'INT' in kind.upper() or 'REAL' in kind.upper() else '' if 'TEXT' in kind.upper() else b'') if required else None)
        c.execute('INSERT INTO '+pm.quote(table)+' ('+','.join(map(pm.quote,record))+') VALUES ('+','.join('?' for _ in record)+')',list(record.values()))

    def state(self,c,name,key,value):
        c.execute('INSERT INTO state VALUES(?,?,?,0)',(name,key,json.dumps(value)))

    def digests(self):
        with pm.connect(self.core) as c,pm.connect(self.desktop) as d:return f.digest(c),f.digest(d)

    def remove(self,**kw):
        return f.remove_project(self.pid,self.core,self.desktop,self.base/'backups',**kw)

    def test_full_removal_preserves_other_project_files_and_restores(self):
        before=self.digests(); files=(self.directory/'keep.txt').read_bytes()
        tree_before={str(p.relative_to(self.directory)):p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}
        plan=f.preview(self.pid,self.core,self.desktop)
        self.assertEqual(len(plan['session_ids']),2)
        folder=self.remove(expected=plan)
        with pm.connect(self.core) as c:
            self.assertIsNone(c.execute('SELECT * FROM project WHERE id=?',(self.pid,)).fetchone())
            self.assertEqual(c.execute('SELECT id FROM session_v2').fetchall()[0][0],self.sb)
            for table in ('session_message','session_part','session_inbox','instruction_entry','instruction_state','schedule','job','event_sequence','event'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],1,table)
            self.assertEqual([r[0] for r in c.execute('SELECT hash FROM blob')],['shared'])
            self.assertEqual(c.execute('SELECT count(*) FROM workspace').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT value FROM credential').fetchone()[0],'KEEP SECRET')
        with pm.connect(self.desktop) as c:
            server=json.loads(c.execute("SELECT value FROM state WHERE key='server'").fetchone()[0])
            self.assertEqual(server['usedProjectIDs']['local'],[self.other])
            self.assertEqual(server['usedProjectIDs']['remote'],[self.pid])
            tabs=json.loads(c.execute("SELECT value FROM state WHERE key='tabs'").fetchone()[0])
            self.assertEqual(len(tabs),2)
            self.assertEqual(tabs[1]['server'],'remote')
            self.assertEqual(json.loads(c.execute("SELECT value FROM state WHERE key='tabs.closed'").fetchone()[0]),[])
            self.assertEqual(c.execute('SELECT count(*) FROM document').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM blob').fetchone()[0],0)
        self.assertEqual((self.directory/'keep.txt').read_bytes(),files)
        self.assertEqual({str(p.relative_to(self.directory)):p.read_bytes() for p in self.directory.rglob('*') if p.is_file()},tree_before)
        f.restore(folder/'undo.json',self.core,self.desktop)
        self.assertEqual(self.digests(),before)
        self.assertEqual((self.directory/'keep.txt').read_bytes(),files)

    def test_instruction_and_shared_draft_blobs(self):
        with pm.connect(self.core,True) as c:
            for h in ('instruction-unique','instruction-shared'):
                self.insert(c,'instruction_blob',hash=h,value='private instruction')
            c.execute('UPDATE instruction_state SET initial_values=? WHERE session_id=?', (json.dumps({'a':'instruction-unique','b':'instruction-shared'}),self.sid))
            c.execute('UPDATE instruction_state SET initial_values=? WHERE session_id=?',(json.dumps({'a':'instruction-shared'}),self.sb))
        with pm.connect(self.desktop,True) as c:
            c.execute('INSERT INTO document VALUES(?,?)',('other-document',json.dumps({'blob':'draft-blob'})))
        folder=self.remove()
        with pm.connect(self.core) as c:
            self.assertEqual([r[0] for r in c.execute('SELECT hash FROM instruction_blob')],['instruction-shared'])
        with pm.connect(self.desktop) as c:self.assertEqual(c.execute('SELECT count(*) FROM blob').fetchone()[0],1)
        f.restore(folder/'undo.json',self.core,self.desktop)

    def test_same_count_content_change_requires_new_preview(self):
        plan=f.preview(self.pid,self.core,self.desktop)
        with pm.connect(self.core,True) as c:c.execute("UPDATE session_message SET data='changed' WHERE session_id=?",(self.sid,))
        with self.assertRaisesRegex(ValueError,'变化'):self.remove(expected=plan)

    def test_no_overwriting_later_data_on_restore(self):
        folder=self.remove()
        with pm.connect(self.desktop,True) as c:self.state(c,'new','new',{'new':'settings'})
        after=self.digests()
        with self.assertRaisesRegex(ValueError,'已变化'):f.restore(folder/'undo.json',self.core,self.desktop)
        self.assertEqual(self.digests(),after)

    def test_both_databases_rollback_if_desktop_write_fails(self):
        before=self.digests(); real=f.apply
        def fail(c,plan):
            real(c,plan)
            raise RuntimeError('simulated failure before commit')
        with patch.object(f,'apply',side_effect=fail):
            with self.assertRaises(RuntimeError):self.remove()
        self.assertEqual(self.digests(),before)

    def test_failed_backup_never_deletes(self):
        before=self.digests()
        with patch.object(f,'snapshot',side_effect=OSError('full disk')):
            with self.assertRaises(OSError):self.remove()
        self.assertEqual(self.digests(),before)

    def test_cross_project_child_blocks_removal(self):
        with pm.connect(self.core,True) as c:c.execute('UPDATE session_v2 SET parent_id=? WHERE id=?',(self.sid,self.sb))
        before=self.digests()
        with self.assertRaisesRegex(ValueError,'跨项目'):self.remove()
        self.assertEqual(self.digests(),before)

    def test_unknown_desktop_schema_and_global_blocked(self):
        with self.assertRaisesRegex(ValueError,'全局'):f.preview('global',self.core,self.desktop)
        with pm.connect(self.desktop,True) as c:c.execute('CREATE TABLE unknown(x TEXT)')
        before=self.digests()
        with self.assertRaisesRegex(ValueError,'结构'):self.remove()
        self.assertEqual(self.digests(),before)

    def test_preview_detects_changed_history(self):
        plan=f.preview(self.pid,self.core,self.desktop)
        with pm.connect(self.core,True) as c:self.insert(c,'session_message',id='newmsg',session_id=self.sid,data='{}')
        before=self.digests()
        with self.assertRaisesRegex(ValueError,'变化'):self.remove(expected=plan)
        self.assertEqual(self.digests(),before)

    def test_wal_backups_include_committed_history(self):
        for path in (self.core,self.desktop):
            with pm.connect(path,True) as c:c.execute('PRAGMA journal_mode=WAL')
        before=self.digests();folder=self.remove()
        with pm.connect(folder/'opencode.db') as c:self.assertEqual(f.digest(c),before[0])
        f.restore(folder/'undo.json',self.core,self.desktop)
        self.assertEqual(self.digests(),before)


if __name__=='__main__':unittest.main()
