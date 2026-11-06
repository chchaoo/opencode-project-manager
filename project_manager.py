"""Conservative offline manager for the locally inspected OpenCode 2.2.5 schema."""
import csv
import io
import json
import hashlib
import os
import sys
import uuid
from pathlib import Path
import sqlite3
import subprocess
from contextlib import closing
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from datetime import datetime

CORE = Path.home() / '.local/share/opencode/opencode.db'
DRAFTS = Path(os.environ.get('APPDATA', '')) / 'ai.opencode.desktop/drafts.sqlite'
VERSION = '0.2.0'
BACKUPS = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ProjectRecordManager' / 'backups'
# Exact table definitions from the inspected OpenCode 2.2.5 installation.
# Unknown schemas remain readable, but writes fail closed.
SUPPORTED_SCHEMA = 'f33b0ac8f509b571c269bddf985c4ee81eebb286de097c25445d2cc18b3dbe02'


def schema_digest(c):
    rows = [tuple(r) for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=True).encode()).hexdigest()


def check_schema(c):
    if schema_digest(c) != SUPPORTED_SCHEMA:
        raise ValueError('未验证的 OpenCode 数据库结构：仅可浏览，禁止修改。当前验证基线为 2.2.5。')
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
        raise ValueError('数据库含有触发器，当前工具不支持。')


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path, write=False):
    c = sqlite3.connect(path.resolve().as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=3, factory=Connection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


def stopped():
    if os.name != 'nt':
        raise ValueError('删除和撤销仅支持 Windows。')
    result = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True, timeout=15)
    names = [r[0].lower() for r in csv.reader(io.StringIO(result.stdout.decode(errors='replace'))) if r]
    if any('opencode' in n for n in names):
        raise ValueError('请先完全退出 OpenCode（包括后台服务和命令行），再操作。工具不会替你关闭进程。')


def added_ids():
    with connect(DRAFTS) as c:
        r = c.execute("SELECT value FROM state WHERE name='opencode.global.dat' AND key='server'").fetchone()
    if not r:
        raise ValueError('无法识别桌面项目状态，为保护项目，禁止删除。')
    data = json.loads(r[0])
    ids = data.get('usedProjectIDs', {}).get('local')
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise ValueError('项目状态格式已变化，禁止删除。')
    # Other versions may use another active-project structure. Do not guess.
    if data.get('projects'):
        raise ValueError('检测到其他项目状态结构，请先适配工具。')
    return set(ids), data.get('lastProjectID', {}).get('local')


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def inspect(c, pid):
    p = c.execute('SELECT * FROM project WHERE id=?', (pid,)).fetchone()
    if not p:
        raise ValueError('项目已不存在，请刷新。')
    p = dict(p)
    counts = {}
    for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        table = row[0]
        cols = [x[1] for x in c.execute('PRAGMA table_info(' + quote(table) + ')')]
        if 'project_id' in cols:
            counts[table] = c.execute('SELECT count(*) FROM ' + quote(table) + ' WHERE project_id=?', (pid,)).fetchone()[0]
    return p, counts


def eligible(c, pid):
    check_schema(c)
    used, last = added_ids()
    p, counts = inspect(c, pid)
    if pid in used or pid == last or pid == 'global' or p['worktree'] in ('/', ''):
        raise ValueError('这是已添加、当前或全局项目，受到保护。')
    if json.loads(p.get('sandboxes') or '[]'):
        raise ValueError('项目存在 sandbox，禁止删除。')
    occupied = {k: v for k, v in counts.items() if v}
    if occupied:
        raise ValueError('项目有依赖记录，禁止删除：' + json.dumps(occupied, ensure_ascii=False))
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'event_sequence' in tables and c.execute('SELECT 1 FROM event_sequence WHERE aggregate_id=? OR owner_id=?', (pid, pid)).fetchone():
        raise ValueError('项目存在事件记录，禁止删除。')
    # Refuse unknown triggers: raw SQL must not cause unexpected side effects.
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
        raise ValueError('数据库含有触发器，当前工具不支持。')
    if c.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('数据库已有外键异常，禁止修改。')
    return p


def delete_empty(pid, db=CORE, backup_root=BACKUPS):
    stopped()
    with connect(db, True) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            p = eligible(c, pid)
            folder = backup_root / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12])
            folder.mkdir(parents=True, exist_ok=False)
            # A separate read connection avoids backing up a connection holding a write transaction.
            with connect(db) as source, closing(sqlite3.connect(folder / 'opencode.db')) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('备份校验失败，取消删除。')
            manifest = {'database': str(db.resolve()), 'project': p}
            (folder / 'undo.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            stopped()
            eligible(c, pid)
            c.execute('DELETE FROM project WHERE id=?', (pid,))
            if c.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('外键校验失败，操作已回滚。')
            c.commit()
            return folder
        except Exception:
            c.rollback()
            raise


def undo(path, db=CORE):
    stopped()
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if Path(data['database']).resolve() != db.resolve():
        raise ValueError('备份不属于此数据库。')
    p = data['project']
    # Restore only an unchanged row from the matching SQLite backup, never arbitrary JSON.
    with connect(Path(path).parent / 'opencode.db') as source:
        check_schema(source)
        original = source.execute('SELECT * FROM project WHERE id=?', (p['id'],)).fetchone()
        if original is None or dict(original) != p:
            raise ValueError('撤销记录与原始数据库备份不一致。')
    with connect(db, True) as c:
        c.execute('BEGIN IMMEDIATE')
        check_schema(c)
        cols = [x[1] for x in c.execute('PRAGMA table_info(project)')]
        if set(cols) != set(p):
            raise ValueError('数据库结构已变化，不能自动撤销。')
        c.execute('INSERT INTO project (' + ','.join(map(quote, cols)) + ') VALUES (' + ','.join('?' for _ in cols) + ')', [p[k] for k in cols])
        if c.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('外键校验失败。')
        c.commit()


class App:
    def __init__(self, root):
        self.root = root
        root.title('OpenCode 项目管理器 v' + VERSION)
        root.geometry('1040x610')
        root.minsize(880, 500)
        ttk.Label(root, text='完整移除项目及其聊天记录；保留项目文件夹和全部文件。先预览、备份，再确认。', padding=12).pack(anchor='w')
        bar = ttk.Frame(root, padding=(12, 0)); bar.pack(fill='x')
        self.only = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text='只显示 Other projects', variable=self.only, command=self.refresh).pack(side='left')
        ttk.Button(bar, text='刷新（只读）', command=self.refresh).pack(side='left', padx=8)
        ttk.Button(bar, text='完整移除项目及聊天…', command=self.remove).pack(side='left')
        ttk.Button(bar, text='撤销某次删除…', command=self.restore).pack(side='left', padx=8)
        self.tree = ttk.Treeview(root, columns=('name', 'path', 'status', 'sessions', 'safe'), show='headings', selectmode='browse')
        for key, title, width in [('name','项目',150),('path','目录',390),('status','分类',100),('sessions','会话数',70),('safe','删除状态',210)]:
            self.tree.heading(key, text=title); self.tree.column(key, width=width)
        self.tree.pack(fill='both', expand=True, padx=12, pady=(12,0))
        scrollbar = ttk.Scrollbar(root, orient='horizontal', command=self.tree.xview)
        scrollbar.pack(fill='x', padx=12)
        self.tree.configure(xscrollcommand=scrollbar.set)
        scrollbar_y = ttk.Scrollbar(self.tree, orient='vertical', command=self.tree.yview)
        scrollbar_y.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scrollbar_y.set)
        self.tree.bind('<<TreeviewSelect>>', self.details)
        self.info = tk.StringVar()
        ttk.Label(root, textvariable=self.info, wraplength=990, padding=12).pack(anchor='w')
        ttk.Label(root, text='移除会清理项目聊天、任务及桌面状态；文件夹内所有文件原样保留。以后可重新添加。备份和历史日志保留。', padding=12).pack(anchor='w')
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children()); self.rows = {}
        try:
            used, last = added_ids()
            with connect(CORE) as c:
                for row in c.execute('SELECT id FROM project').fetchall():
                    pid = row[0]; p, counts = inspect(c, pid)
                    is_other = pid not in used
                    if self.only.get() and not is_other: continue
                    try:
                        from full_cleanup import preview
                        plan = preview(pid)
                        safe = '可完整移除（含聊天）'; reason = '涉及目录：' + '；'.join(plan['paths'])
                    except Exception as e: safe = '受保护 / 有依赖'; reason = str(e)
                    sessions = sum(v for k,v in counts.items() if k in ('session', 'session_v2'))
                    self.rows[pid] = (p, counts, reason)
                    self.tree.insert('', 'end', iid=pid, values=(p['name'] or Path(p['worktree']).name, p['worktree'], 'Other projects' if is_other else '已添加', sessions, safe))
            self.info.set(f'已只读加载 {len(self.rows)} 个项目。数据库：{CORE}')
        except Exception as e: self.info.set('读取失败：' + str(e))

    def details(self, _=None):
        if self.tree.selection():
            pid = self.tree.selection()[0]; p, counts, reason = self.rows[pid]
            self.info.set('项目 ID：' + pid + '\n关联记录：' + json.dumps(counts, ensure_ascii=False) + ('\n' + reason if reason else ''))

    def remove(self):
        if not self.tree.selection(): return
        pid = self.tree.selection()[0]
        try:
            from full_cleanup import preview, remove_project
            plan = preview(pid); p = plan['project']
            stopped()
            desktop = plan['desktop']
            detail = (f"目录：{p['worktree']}\nID：{pid}\n\n"
                      f"将移除 {len(plan['session_ids'])} 个会话、{plan['messages']} 条消息及关联的任务、权限、事件记录。\n"
                      f"清理 {len(desktop['removed'])+len(desktop['updated'])} 条桌面状态、{len(desktop['documents'])} 份草稿。\n"
                      f"涉及目录：{len(plan['paths'])} 个（同项目的工作树/克隆登记一并移除）。\n\n"
                      "项目文件夹及里面全部文件保留。自动备份两个数据库，备份含私密数据。\n"
                      "整库撤销仅限移除后数据库尚未发生变化时。\n\n请输入“移除”以确认：")
            if simpledialog.askstring('预览并确认完整移除',detail,parent=self.root) != '移除': return
            folder = remove_project(pid,expected=plan)
            self.refresh()
            messagebox.showinfo('已删除', '备份和撤销文件保存在：\n' + str(folder), parent=self.root)
        except Exception as e: messagebox.showerror('未删除', str(e), parent=self.root)

    def restore(self):
        path = filedialog.askopenfilename(title='选择该次删除的 undo.json', initialdir=BACKUPS, filetypes=[('撤销记录', 'undo.json')])
        if not path: return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            full = data.get('format') == 2
            note = ('恢复删除前的两个数据库。仅在两库仍与删除后完全一致时允许；已有新数据会拒绝恢复。'
                    if full else '仅恢复该条空项目记录，不覆盖整个数据库。')
            if messagebox.askyesno('撤销删除', '恢复项目：\n' + data['project']['worktree'] + '\n\n' + note, parent=self.root):
                if full:
                    from full_cleanup import restore
                    restore(path)
                else: undo(path)
                self.refresh()
        except Exception as e: messagebox.showerror('无法撤销', str(e), parent=self.root)


if __name__ == '__main__':
    root = tk.Tk()
    def report_error(kind, value, traceback):
        messagebox.showerror('操作失败', str(value), parent=root)
    root.report_callback_exception = report_error
    App(root)
    if '--smoke-test' in sys.argv:
        root.after(1500, root.destroy)
    root.mainloop()
