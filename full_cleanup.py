"""Offline project removal for the verified OpenCode 2.2.5 schemas.

Only the two SQLite stores are modified. The project directory is never opened
for writing. Core + desktop changes use SQLite's rollback-journal super-journal.
"""
import base64
from contextlib import contextmanager, closing
from datetime import datetime
import hashlib
import json
import ntpath
from pathlib import Path
import re
import sqlite3
import uuid

import project_manager as pm

DESKTOP_SCHEMA = '3652493fa2197014b0ef17a7542e746372ba1c283314c3f083e6ff9bf436e5e6'
DROP = object()


def normalized(path):
    return ntpath.normcase(ntpath.normpath(path)).replace('\\', '/')


def storage_name(kind, value):
    # Matches the installed renderer: UTF-16 FNV-1a, unsigned base36.
    raw = value.encode('utf-16-le', errors='surrogatepass')
    h = 2166136261
    for i in range(0, len(raw), 2):
        h = ((h ^ int.from_bytes(raw[i:i+2], 'little')) * 16777619) & 0xffffffff
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'; result = ''
    while h:
        result = digits[h % 36] + result; h //= 36
    prefix = raw[:24].decode('utf-16-le', errors='surrogatepass') or kind
    prefix = re.sub('[^a-zA-Z0-9._-]', '-', prefix)
    return f'opencode.{kind}.{prefix}.{result or "0"}.dat'


def tables(c, schema='main'):
    return [r[0] for r in c.execute(f'SELECT name FROM {schema}.sqlite_master WHERE type="table" ORDER BY name')]


def digest(c, schema='main'):
    h = hashlib.sha256()
    def encoded(value):
        if isinstance(value, bytes): return {'bytes': base64.b64encode(value).decode('ascii')}
        return value
    for table in tables(c, schema):
        h.update(table.encode())
        # Content-order independent, includes all columns, including blobs.
        statement = c.execute(f'SELECT * FROM {schema}.{pm.quote(table)}')
        h.update(json.dumps([v[0] for v in statement.description]).encode())
        row_hashes = []
        for row in statement:
            serialized = json.dumps([encoded(v) for v in row], ensure_ascii=True, separators=(',', ':'))
            row_hashes.append(hashlib.sha256(serialized.encode()).digest())
        for row in sorted(row_hashes): h.update(row)
    return h.hexdigest()


def desktop_check(c):
    if pm.schema_digest(c) != DESKTOP_SCHEMA:
        raise ValueError('桌面数据库结构未经验证，禁止修改。')
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
        raise ValueError('桌面数据库含未知触发器，禁止修改。')
    if c.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('桌面数据库外键异常。')


def rows(c, table):
    return [dict(r) for r in c.execute('SELECT * FROM ' + pm.quote(table))]


def row_where(c, table, column, values):
    # One bound value per statement avoids SQLite's parameter limit on large histories.
    output = []
    for value in sorted(values):
        output.extend(dict(r) for r in c.execute(f'SELECT * FROM {pm.quote(table)} WHERE {pm.quote(column)}=?', (value,)))
    return output


def get_plan(c, d, pid):
    pm.check_schema(c); desktop_check(d)
    if c.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('数据库已有外键异常。')
    p, counts = pm.inspect(c, pid)
    if pid == 'global' or not p['worktree'] or normalized(p['worktree']) in ('/', '.', '\\'):
        raise ValueError('全局项目不能移除。')
    sessions = row_where(c, 'session_v2', 'project_id', {pid})
    ids = {s['id'] for s in sessions}
    for s in rows(c, 'session_v2'):
        if s['id'] not in ids and (s['parent_id'] in ids or s['fork_session_id'] in ids):
            raise ValueError('其他项目的会话依赖此项目的会话，拒绝跨项目删除。')
    for stream in row_where(c,'event_sequence','owner_id',{pid}):
        foreign = c.execute('SELECT project_id FROM session_v2 WHERE id=?',(stream['aggregate_id'],)).fetchone()
        if foreign and foreign[0] != pid:
            raise ValueError('存在其他项目共享的事件流，禁止删除。')
    for job in rows(c,'job'):
        if job['child_session_id'] in ids and job['session_id'] not in ids:
            raise ValueError('其他项目的后台任务引用了目标会话，禁止跨项目删除。')
    paths = {p['worktree']} | {s['directory'] for s in sessions}
    for table in ('worktree', 'project_directory'):
        paths.update(r['directory'] for r in row_where(c, table, 'project_id', {pid}))
    paths.update(json.loads(p['sandboxes'] or '[]'))
    if not all(isinstance(v, str) for v in paths): raise ValueError('无法识别项目路径。')
    # A directory namespace can be shared by a different project. Never erase it blindly.
    other_paths = {normalized(r['worktree']) for r in rows(c, 'project') if r['id'] != pid}
    other_paths.update(normalized(s['directory']) for s in rows(c,'session_v2') if s['project_id'] != pid)
    for table in ('worktree', 'project_directory'):
        other_paths.update(normalized(r['directory']) for r in rows(c, table) if r['project_id'] != pid)
    if {normalized(v) for v in paths} & other_paths:
        raise ValueError('目录被其他项目共享，无法安全清理桌面状态。')
    messages = row_where(c, 'session_message', 'session_id', ids)
    inbox = row_where(c, 'session_inbox', 'session_id', ids)
    blobs = {r['hash'] for r in row_where(c, 'blob_ref', 'session_id', ids)}
    for r in messages + inbox:
        blobs.update(re.findall(r'media:([a-fA-F0-9]{64})', r.get('data', '') + r.get('payload', '')))
    workspaces = {s['workspace_id'] for s in sessions if s['workspace_id']}
    instruction_data = row_where(c,'instruction_state','session_id',ids)
    streams = [pid] + list(ids)
    instruction_text = '\n'.join(str(r) for r in instruction_data)
    for identity in streams:
        instruction_text += '\n'.join(r['data'] for r in row_where(c,'event','aggregate_id',{identity}))
    instruction_hashes = [r['hash'] for r in c.execute('SELECT hash FROM instruction_blob') if r['hash'] in instruction_text]
    desktop = desktop_plan(d, pid, ids, paths)
    # Untyped KV data is not safe to infer. Refuse if it contains target IDs/paths.
    tokens = {pid} | ids | paths
    for r in rows(c, 'kv'):
        if any(t in r['key'] or t in r['value'] for t in tokens):
            raise ValueError('发现无法确定归属的 KV 记录，需适配后再移除。')
    return {'project': p, 'session_ids': sorted(ids), 'paths': sorted(paths),
            'counts': counts, 'messages': len(messages), 'blob_hashes': sorted(blobs),
            'workspace_ids': sorted(workspaces), 'instruction_hashes':sorted(instruction_hashes),
            'desktop': desktop, 'fingerprints': {'core':digest(c),'desktop':digest(d)}}


def desktop_plan(c, pid, sessions, paths):
    """Change exact local project/session namespaces; never substring-match directories."""
    pathset = {normalized(p) for p in paths}
    namespaces = set()
    for p in paths:
        forward = p.replace('\\', '/')
        for spelling in {p, forward, forward.replace('/', '\\')}:
            namespaces.add(storage_name('workspace', spelling))
    states = rows(c, 'state'); documents = rows(c, 'document')
    draft_ids = set()
    def in_path(value):
        return isinstance(value, str) and normalized(value) in pathset
    def is_tab(value):
        if not isinstance(value, dict) or value.get('server') != 'local': return False
        if value.get('type') == 'session':
            return any(value.get(k) in sessions for k in ('sessionId', 'routeSessionId', 'routeParentId'))
        if value.get('type') == 'draft' and (in_path(value.get('directory')) or in_path(value.get('worktree'))):
            if not isinstance(value.get('draftID'), str): raise ValueError('草稿 ID 格式异常。')
            draft_ids.add(value['draftID']); return True
        return False
    # Discover drafts from both open and closed window tabs before removing namespaces.
    for r in states:
        if r['name'].startswith('opencode.window.') and r['key'] in ('tabs', 'tabs.closed'):
            data = json.loads(r['value'])
            if not isinstance(data, list): raise ValueError('标签页状态格式已变化。')
            for item in data: is_tab(item.get('tab') if isinstance(item, dict) and 'tab' in item else item)
    namespaces.update(storage_name('draft', value) for value in draft_ids)
    local_route = base64.urlsafe_b64encode(b'local').decode().rstrip('=')
    routes = {'/server/' + server + '/session/' + sid for sid in sessions for server in ('local',local_route)}
    tabkeys = {'local\n' + route for route in routes} | {'draft:' + value for value in draft_ids}
    removed = []; updated = []; doc_removed = []
    def target_string(value):
        return value in sessions or value == pid or value in tabkeys or value in routes or in_path(value)
    def clean(value, local=True):
        if isinstance(value, dict):
            if isinstance(value.get('server'), str) and value['server'] != 'local': return value
            if is_tab(value) or is_tab(value.get('tab')): return DROP
            if value.get('projectID') == pid or any(in_path(value.get(k)) for k in ('directory','worktree')):
                return DROP
            result = {}
            for k, v in value.items():
                if '\0' in k and not k.startswith('local\0'):
                    result[k] = v; continue
                if k in tabkeys or k in routes or k in sessions or k == pid: continue
                if in_path(k): continue
                # Remote-scoped entries use NUL separators; do not match their suffixes.
                if k.startswith('local\0') and (k[6:] in routes or k[6:] in sessions): continue
                next_value = clean(v, local)
                if next_value is not DROP: result[k] = next_value
            return result
        if isinstance(value, list):
            result = []
            for v in value:
                next_value = clean(v, local)
                if next_value is not DROP: result.append(next_value)
            return result
        if isinstance(value, str) and target_string(value): return DROP
        return value
    for r in states:
        if r['name'] in namespaces:
            removed.append(r); continue
        data = json.loads(r['value'])
        after = data
        if r['name'] == 'opencode.global.dat' and r['key'] == 'server':
            if not isinstance(data, dict): raise ValueError('项目状态异常。')
            after = json.loads(r['value'])
            for key in ('usedProjectIDs', 'lastProjectID', 'recentlyClosed', 'projects'):
                mapping = after.get(key, {})
                if not isinstance(mapping, dict): raise ValueError('未知项目状态格式。')
                if 'local' in mapping:
                    changed = clean(mapping['local'])
                    if changed is DROP: del mapping['local']
                    else: mapping['local'] = changed
        elif r['name'].startswith('opencode.window.') or (r['name'] == 'opencode.global.dat' and r['key'] == 'layout'):
            after = clean(data)
            if after is DROP: after = None
        # Any other exact session/project reference is unknown and must not silently survive.
        elif r['name'] not in namespaces:
            if contains_identity(data, {pid} | sessions):
                raise ValueError('发现未知桌面状态引用：' + r['name'] + ':' + r['key'])
        if after != data:
            updated.append({'before': r, 'after': json.dumps(after, ensure_ascii=True, separators=(',', ':'))})
    for r in documents:
        if any(r['key'].startswith(n + ':') for n in namespaces):
            doc_removed.append(r)
        elif contains_identity(json.loads(r['value']), {pid} | sessions) or any(sid in r['key'] for sid in sessions):
            # Known scoped session document keys below workspace namespaces are handled above.
            raise ValueError('发现无法确定归属的草稿文档，需适配后再移除。')
    # Reclaim only blobs demonstrably referenced by removed documents, never
    # blobs still mentioned by a retained document/state.
    removed_keys = {r['key'] for r in doc_removed}
    retained = [r['value'] for r in documents if r['key'] not in removed_keys]
    removed_state_keys = {(r['name'],r['key']) for r in removed}
    changes = {(r['before']['name'],r['before']['key']):r['after'] for r in updated}
    retained += [changes.get((r['name'],r['key']),r['value']) for r in states if (r['name'],r['key']) not in removed_state_keys]
    removed_text = '\n'.join(r['value'] for r in doc_removed)
    blob_ids = [r['id'] for r in c.execute('SELECT id FROM blob') if r['id'] in removed_text and not any(r['id'] in s for s in retained)]
    return {'removed': removed, 'updated': updated, 'documents': doc_removed, 'blob_ids':blob_ids}


def contains_identity(value, identities):
    if isinstance(value, str): return value in identities
    if isinstance(value, dict): return any(k in identities or contains_identity(v, identities) for k,v in value.items())
    if isinstance(value, list): return any(contains_identity(v, identities) for v in value)
    return False


def preview(pid, core=None, desktop=None):
    with pm.connect(core or pm.CORE) as c, pm.connect(desktop or pm.DRAFTS) as d:
        return get_plan(c, d, pid)


def snapshot(source, destination):
    with pm.connect(source) as c, closing(sqlite3.connect(destination)) as target:
        c.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('备份校验失败。')


@contextmanager
def transaction(core, desktop):
    """Both file databases in DELETE mode: SQLite super-journal gives atomic commit.

    WAL mode cannot guarantee atomic multi-database commits, so switch offline,
    then restore original modes on close. No project files are touched.
    """
    modes = []
    with pm.connect(core, True) as c:
        try:
            for path in (core, desktop):
                with pm.connect(path, True) as temp:
                    mode = temp.execute('PRAGMA journal_mode').fetchone()[0]
                    modes.append((path, mode))
                    if temp.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                        raise ValueError('无法切换离线事务模式。')
            c.execute('ATTACH DATABASE ? AS desktop', (str(desktop.resolve()),))
            c.execute('PRAGMA synchronous=FULL'); c.execute('PRAGMA desktop.synchronous=FULL')
            c.execute('BEGIN IMMEDIATE')
            yield c
            if c.execute('PRAGMA foreign_key_check').fetchone() or c.execute('PRAGMA desktop.foreign_key_check').fetchone():
                raise ValueError('外键校验失败，已回滚。')
            pm.stopped()
            c.commit()
        except Exception:
            c.rollback(); raise
        finally:
            try: c.execute('DETACH DATABASE desktop')
            except sqlite3.Error: pass
            for path, mode in modes:
                try:
                    with pm.connect(path, True) as temp:
                        temp.execute('PRAGMA journal_mode=' + ('WAL' if mode.lower() == 'wal' else 'DELETE'))
                except sqlite3.Error:
                    # Committed data must not be reported as "not deleted" just
                    # because restoring this performance setting failed. DELETE
                    # is a valid, durable journal mode understood by SQLite.
                    pass


def apply(c, plan):
    pid = plan['project']['id']; ids = plan['session_ids']
    for sid in ids:
        c.execute('DELETE FROM job WHERE session_id=? OR child_session_id=?', (sid, sid))
    for identity in [pid] + ids:
        c.execute('DELETE FROM event_sequence WHERE aggregate_id=? OR owner_id=?', (identity, identity))
    c.execute('DELETE FROM project WHERE id=?', (pid,))
    # Core foreign keys cascade sessions/messages/parts/inbox/instructions/schedules.
    for value in plan['workspace_ids']:
        c.execute('DELETE FROM workspace WHERE id=? AND NOT EXISTS (SELECT 1 FROM session_v2 WHERE workspace_id=?)', (value,value))
    for h in plan['blob_hashes']:
        # Shared refs and pending inbox media keep a blob alive.
        c.execute('DELETE FROM blob WHERE hash=? AND NOT EXISTS (SELECT 1 FROM blob_ref WHERE hash=?) '
                  'AND NOT EXISTS (SELECT 1 FROM session_inbox WHERE instr(payload,?)>0) '
                  'AND NOT EXISTS (SELECT 1 FROM session_message WHERE instr(data,?)>0)', (h,h,h,h))
    for h in plan['instruction_hashes']:
        c.execute('DELETE FROM instruction_blob WHERE hash=? '
                  'AND NOT EXISTS (SELECT 1 FROM instruction_state WHERE instr(initial_values,?)>0 OR instr(current_values,?)>0) '
                  'AND NOT EXISTS (SELECT 1 FROM event WHERE instr(data,?)>0) '
                  'AND NOT EXISTS (SELECT 1 FROM instruction_entry WHERE instr(value,?)>0)', (h,h,h,h,h))
    for r in plan['desktop']['removed']:
        c.execute('DELETE FROM desktop.state WHERE name=? AND key=?', (r['name'],r['key']))
    for change in plan['desktop']['updated']:
        r = change['before']
        c.execute('UPDATE desktop.state SET value=? WHERE name=? AND key=?', (change['after'],r['name'],r['key']))
    for r in plan['desktop']['documents']:
        c.execute('DELETE FROM desktop.document WHERE key=?', (r['key'],))
    for value in plan['desktop']['blob_ids']:
        c.execute('DELETE FROM desktop.blob WHERE id=?', (value,))


def remove_project(pid, core=None, desktop=None, backup_root=None, expected=None):
    core = core or pm.CORE; desktop = desktop or pm.DRAFTS; backup_root = backup_root or pm.BACKUPS
    pm.stopped()
    plan = preview(pid, core, desktop)
    if expected is not None and expected != plan: raise ValueError('项目数据已变化，请重新预览。')
    folder = backup_root / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    manifest = {'format': 2, 'database': str(core.resolve()), 'desktop': str(desktop.resolve()),
                'project': plan['project'], 'status': 'prepared'}
    snapshot(core, folder/'opencode.db'); snapshot(desktop, folder/'drafts.sqlite')
    manifest['backup_hashes'] = {name: hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in ('opencode.db','drafts.sqlite')}
    save = lambda: (folder/'undo.json').write_text(json.dumps(manifest, ensure_ascii=True, indent=2),encoding='utf-8')
    save()
    with transaction(core, desktop) as c:
        with pm.connect(desktop) as d:
            current = get_plan(c,d,pid)
        if current != plan: raise ValueError('数据已变化，已取消。')
        # Prove the backup corresponds exactly to the locked pre-delete contents.
        for schema, name in [('main','opencode.db'),('desktop','drafts.sqlite')]:
            with pm.connect(folder/name) as b:
                if digest(c,schema) != digest(b): raise ValueError('备份后数据已变化，已取消。')
        apply(c,plan)
        manifest['after'] = {'core':digest(c), 'desktop':digest(c,'desktop')}
        # Write recovery information before committing either database.
        save()
    manifest['status'] = 'committed'
    try: save()
    except OSError: pass  # Prepared manifest already contains the undo fingerprints.
    return folder


def restore(path, core=None, desktop=None):
    core = core or pm.CORE; desktop = desktop or pm.DRAFTS
    pm.stopped(); path = Path(path)
    m = json.loads(path.read_text(encoding='utf-8'))
    if m.get('format') != 2 or 'after' not in m: raise ValueError('不是完整移除的可撤销备份。')
    if Path(m['database']).resolve()!=core.resolve() or Path(m['desktop']).resolve()!=desktop.resolve():
        raise ValueError('备份路径不匹配。')
    for name in ('opencode.db','drafts.sqlite'):
        if hashlib.sha256((path.parent/name).read_bytes()).hexdigest()!=m['backup_hashes'][name]:
            raise ValueError('备份内容已变化，拒绝恢复。')
    # Whole-store restore is allowed only while BOTH stores still match the
    # post-delete contents. Later chats/settings/another deletion must not be overwritten.
    with transaction(core,desktop) as c:
        if digest(c)!=m['after']['core'] or digest(c,'desktop')!=m['after']['desktop']:
            raise ValueError('移除后 OpenCode 数据已变化，禁止整库撤销，以免覆盖新聊天或设置。')
        pm.check_schema(c)
        with pm.connect(desktop) as current_desktop: desktop_check(current_desktop)
        with pm.connect(path.parent/'opencode.db') as b, pm.connect(path.parent/'drafts.sqlite') as d:
            pm.check_schema(b); desktop_check(d)
            # Deferred FK checks allow restoring cyclic/self-referential graph order.
            c.execute('PRAGMA defer_foreign_keys=ON')
            # Delete referencing tables before their parents; core cascade handles most.
            c.execute('DELETE FROM project'); c.execute('DELETE FROM event_sequence')
            for schema, source in [('main',b),('desktop',d)]:
                names = tables(source)
                # Empty ALL tables first; foreign-key checks are deferred until commit.
                for table in reversed(names): c.execute(f'DELETE FROM {schema}.{pm.quote(table)}')
                for table in names:
                    data = source.execute('SELECT * FROM ' + pm.quote(table))
                    cols = [v[0] for v in data.description]
                    sql = f'INSERT INTO {schema}.{pm.quote(table)} (' + ','.join(map(pm.quote,cols)) + ') VALUES (' + ','.join('?' for _ in cols) + ')'
                    c.executemany(sql, [tuple(row) for row in data])
            if digest(c)!=digest(b) or digest(c,'desktop')!=digest(d): raise ValueError('恢复校验失败。')
    m['status']='restored'; path.write_text(json.dumps(m,ensure_ascii=True,indent=2),encoding='utf-8')
