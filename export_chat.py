"""Local Codex transcript export. Standard library only; no network or model calls."""
import collections
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def read_threads(home):
    databases = sorted(home.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
    for db_path in databases:
        with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
            if not {'id', 'title', 'rollout_path'} <= columns:
                continue
            order = 'updated_at' if 'updated_at' in columns else 'id'
            selected = ['id', 'title', 'rollout_path']
            selected += [c for c in ('updated_at', 'name', 'source', 'thread_source', 'agent_nickname', 'agent_path', 'archived') if c in columns]
            rows = [dict(r) for r in db.execute('SELECT ' + ','.join(selected) + ' FROM threads ORDER BY ' + order + ' DESC')]
            for row in rows:
                source = row.get('source', '')
                try:
                    source_data = json.loads(source) if isinstance(source, str) and source.startswith('{') else {}
                except json.JSONDecodeError:
                    source_data = {}
                subagent = source_data.get('subagent') if isinstance(source_data, dict) else None
                spawn = subagent.get('thread_spawn', {}) if isinstance(subagent, dict) else {}
                row['_subtask'] = row.get('thread_source') == 'subagent' or subagent is not None
                row['_parent_id'] = spawn.get('parent_thread_id', '')
                row['_kind'] = '子任务' if row['_subtask'] else ('语音对话' if row.get('thread_source') == 'voice_chat' else '对话')
                if row.get('name'):
                    row['_display_title'] = str(row['name']).strip()
                elif row['_subtask']:
                    agent_path = row.get('agent_path') or spawn.get('agent_path') or ''
                    nickname = row.get('agent_nickname') or spawn.get('agent_nickname') or ''
                    row['_display_title'] = '子任务：' + (agent_path.rsplit('/', 1)[-1] or nickname or row['id'][:8])
                    if nickname and agent_path:
                        row['_display_title'] += '（' + nickname + '）'
                else:
                    row['_display_title'] = readable_title(row['title'], '未命名' + row['_kind'])
            by_id = {r['id']: r for r in rows}
            for row in rows:
                parent = by_id.get(row['_parent_id'])
                row['_parent_title'] = parent['_display_title'] if parent else ''
            return rows
    raise ValueError('没有找到可读取的会话索引。请选择包含 state_*.sqlite 和 sessions 文件夹的 Codex 数据目录。')


def normalized_id(value):
    return str(value).removeprefix('local:')


def matching_files(home, sid, indexed_path=''):
    sid = normalized_id(sid)
    if not re.fullmatch(r'[a-fA-F0-9-]{16,64}', sid):
        raise ValueError('会话 ID 格式无效。')
    candidates = set()
    # SQLite can use Windows extended paths (\\?\C:\...), unlike rglob results.
    indexed = Path(str(indexed_path).removeprefix('\\\\?\\')) if indexed_path else None
    if indexed is not None and indexed.is_file():
        candidates.add(indexed.resolve())
    for directory in ('sessions', 'archived_sessions'):
        base = home / directory
        if base.is_dir():
            candidates.update(p.resolve() for p in base.rglob('*' + sid + '*.jsonl'))
    matches = []
    for path in sorted(candidates, key=lambda p: str(p)):
        with path.open(encoding='utf-8-sig') as source:
            for line in source:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get('type') == 'session_meta':
                    actual = event.get('payload', {}).get('id', event.get('id', ''))
                    if normalized_id(actual) == sid:
                        matches.append(path)
                    break
    if not matches:
        raise ValueError('没有找到这个对话的本地日志。云端或其他电脑上的记录需要先保存到本机。')
    return matches


def clean_user(text):
    text = re.sub(r'<skill>.*?</skill>', '', text, flags=re.S).strip()
    if text.startswith(('<environment_context>', '<external_codex_apps_', '<permissions instructions>', '# AGENTS.md instructions')):
        return ''
    if text.startswith('<send_user_message_question_reply>'):
        raw = re.sub(r'</?send_user_message_question_reply>', '', text).strip()
        try:
            replies = json.loads(raw)
            if isinstance(replies, list):
                return '\n\n'.join('问题：' + str(v.get('question', '')) + '\n\n回答：' + str(v.get('answer', '')) for v in replies if isinstance(v, dict))
        except json.JSONDecodeError:
            pass
        return raw
    return text


def content_text(content):
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind in ('input_text', 'output_text', 'text'):
            parts.append(str(block.get('text', '')))
        elif kind in ('input_image', 'image'):
            url = block.get('image_url', '')
            if isinstance(url, dict):
                url = url.get('url', '')
            parts.append('> 图片附件（图片本身未嵌入）' + ('\n[图片链接](<' + url + '>)' if isinstance(url, str) and url and not url.startswith('data:') else ''))
        elif 'audio' in str(kind) or 'video' in str(kind):
            parts.append('> 音视频附件（媒体本身未嵌入）')
    return '\n\n'.join(parts).strip()


def parse_time(raw):
    try:
        stamp = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (ValueError, TypeError):
        return None


def collect_messages(files):
    records = []
    warnings = []
    for file_index, path in enumerate(files):
        primary, fallback = [], []
        with path.open(encoding='utf-8-sig') as source:
            for number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    warnings.append(f'{path.name} 第 {number} 行无法解析，已跳过。')
                    continue
                payload = event.get('payload', {})
                if not isinstance(payload, dict):
                    continue
                role, text, destination = None, '', primary
                if event.get('type') == 'response_item' and payload.get('type') == 'message':
                    role = payload.get('role')
                    if payload.get('channel') in ('analysis', 'summary'):
                        continue
                    text = content_text(payload.get('content', []))
                elif event.get('type') == 'event_msg' and payload.get('type') in ('user_message', 'agent_message', 'assistant_message'):
                    role = 'user' if payload['type'] == 'user_message' else 'assistant'
                    text = content_text(payload.get('message', ''))
                    destination = fallback
                if role not in ('user', 'assistant'):
                    continue
                if role == 'user':
                    text = clean_user(text)
                if not text.strip():
                    continue
                destination.append({'role': role, 'text': text, 'timestamp': event.get('timestamp', ''), 'file_index': file_index, 'line': number})
        primary_texts = {(r['role'], r['text']) for r in primary}
        records.extend(primary)
        records.extend(r for r in fallback if (r['role'], r['text']) not in primary_texts)
    seen, unique = set(), []
    for r in records:
        stamp = parse_time(r['timestamp'])
        # Only deduplicate the same timestamp, role and body. Repeated questions remain.
        key = (stamp.isoformat() if stamp else (r['file_index'], r['line']), r['role'], r['text'])
        if key in seen:
            continue
        seen.add(key)
        r['time'] = stamp
        unique.append(r)
    unique.sort(key=lambda r: (r['time'].timestamp() if r['time'] else float('inf'), r['file_index'], r['line']))
    return unique, warnings


def render_markdown(title, sid, files, records, warnings):
    lines = ['# ' + title.strip().replace('\n', ' '), '', '会话 ID：`' + sid + '`', '',
             f'> 已合并本机同一会话的 {len(files)} 个日志片段。保留用户和助手消息，过滤技能/环境注入、工具记录和推理记录。附件仅保留提示或链接。',
             '> 覆盖范围以本机现存日志为准；不获取云端缺失历史，不合并其他会话 ID。', '']
    if warnings:
        lines += ['> 注意：有 ' + str(len(warnings)) + ' 行无法解析，详见文末；这份导出可能有缺失。', '']
    for record in records:
        label = '用户' if record['role'] == 'user' else '助手'
        stamp = record['time'].astimezone().strftime('%Y-%m-%d %H:%M:%S %z') if record['time'] else '时间未记录'
        lines += ['## ' + label + ' · ' + stamp, '', record['text'], '']
    lines += ['---', '', '## 来源文件', ''] + ['- `' + p.name + '`' for p in files] + ['']
    if warnings:
        lines += ['## 读取提示', ''] + ['- ' + v for v in warnings] + ['']
    return '\n'.join(lines)


def readable_title(raw, sid):
    if '## My request:' in (raw or ''):
        raw = raw.split('## My request:', 1)[1]
    title = re.sub(r'\[\$[^\]]+\]\([^\n]*?\)', '', raw or '').strip()
    return ' '.join(title.split()) or sid


class ExportApp:
    def __init__(self, root):
        self.root = root
        self.rows = []
        self.home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        root.title('Codex 对话导出器 · 本地合并导出')
        root.geometry('920x650')
        root.minsize(680, 450)
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='选择对话 → 导出 Markdown', font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='合并同一会话的日志片段；本地运行，不调用 AI。').pack(anchor='w', pady=(5, 12))
        directory = ttk.Frame(frame)
        directory.pack(fill='x')
        self.home_label = ttk.Label(directory, text=str(self.home))
        self.home_label.pack(side='left', fill='x', expand=True)
        ttk.Button(directory, text='更换数据目录', command=self.choose_home).pack(side='right')
        search = ttk.Frame(frame)
        search.pack(fill='x', pady=10)
        ttk.Label(search, text='搜索标题或 ID：').pack(side='left')
        self.query = tk.StringVar()
        entry = ttk.Entry(search, textvariable=self.query)
        entry.pack(side='left', fill='x', expand=True)
        self.query.trace_add('write', lambda *_: self.filter_rows())
        ttk.Button(search, text='刷新', command=self.reload).pack(side='left', padx=(8, 0))
        options = ttk.Frame(frame)
        options.pack(fill='x', pady=(0, 8))
        self.show_subtasks = tk.BooleanVar(value=False)
        self.show_archived = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text='显示 AI 子任务', variable=self.show_subtasks, command=self.filter_rows).pack(side='left')
        ttk.Checkbutton(options, text='显示已归档对话', variable=self.show_archived, command=self.filter_rows).pack(side='left', padx=16)
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill='both', expand=True)
        self.table = ttk.Treeview(table_frame, columns=('title', 'kind', 'date'), show='headings', selectmode='browse')
        self.table.heading('title', text='对话标题')
        self.table.heading('kind', text='类型')
        self.table.heading('date', text='最近更新')
        self.table.column('title', width=540)
        self.table.column('kind', width=120, stretch=False)
        self.table.column('date', width=140, stretch=False)
        scroll = ttk.Scrollbar(table_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.table.pack(side='left', fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', self.selected)
        self.sid_label = ttk.Label(frame, text='选择一条对话，可在下方修改导出标题。')
        self.sid_label.pack(anchor='w', pady=(12, 5))
        title_frame = ttk.Frame(frame)
        title_frame.pack(fill='x')
        ttk.Label(title_frame, text='导出标题：').pack(side='left')
        self.title = tk.StringVar()
        ttk.Entry(title_frame, textvariable=self.title).pack(side='left', fill='x', expand=True)
        footer = ttk.Frame(frame)
        footer.pack(fill='x', pady=(12, 0))
        self.status = tk.StringVar(value='正在读取本地对话索引…')
        ttk.Label(footer, textvariable=self.status, wraplength=650).pack(side='left', fill='x', expand=True)
        self.export_button = ttk.Button(footer, text='导出 Markdown…', command=self.export)
        self.export_button.pack(side='right')
        root.after(50, self.reload)
        entry.focus_set()

    def choose_home(self):
        folder = filedialog.askdirectory(title='选择 Codex 数据目录', initialdir=str(self.home))
        if folder:
            self.home = Path(folder)
            self.home_label.configure(text=str(self.home))
            self.reload()

    def reload(self):
        try:
            self.rows = read_threads(self.home)
            self.filter_rows()
        except Exception as exc:
            self.status.set('无法读取索引')
            messagebox.showerror('读取失败', str(exc))

    def filter_rows(self):
        query = self.query.get().casefold().strip()
        self.table.delete(*self.table.get_children())
        for index, row in enumerate(self.rows):
            if row.get('_subtask') and not self.show_subtasks.get():
                continue
            if row.get('archived') and not self.show_archived.get():
                continue
            if query and query not in (str(row['title']) + ' ' + row['_display_title'] + ' ' + row['_parent_title'] + ' ' + row['id']).casefold():
                continue
            stamp = row.get('updated_at', 0) or 0
            try:
                stamp = float(stamp)
                if stamp > 100_000_000_000:
                    stamp /= 1000
                date = datetime.fromtimestamp(stamp).strftime('%m-%d %H:%M') if stamp else ''
            except (ValueError, OverflowError, OSError):
                date = ''
            kind = row['_kind'] + (' · 已归档' if row.get('archived') else '')
            self.table.insert('', 'end', iid=str(index), values=(row['_display_title'], kind, date))
        self.status.set(f'显示 {len(self.table.get_children())} 条对话；数据只读。')

    def selected(self, _=None):
        chosen = self.table.selection()
        if chosen:
            row = self.rows[int(chosen[0])]
            self.title.set(row['_display_title'])
            extra = '\n所属对话：' + (row['_parent_title'] or row['_parent_id']) if row['_parent_id'] else ''
            self.sid_label.configure(text='会话 ID：' + row['id'] + extra)

    def export(self):
        chosen = self.table.selection()
        if not chosen:
            messagebox.showinfo('选择对话', '请先点击列表中的一条对话。')
            return
        row = dict(self.rows[int(chosen[0])])
        title = self.title.get().strip() or row['id']
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', title)[:80].strip(' .') or 'Codex对话'
        destination = filedialog.asksaveasfilename(title='保存合并后的对话', initialdir=str(Path.home() / 'Downloads'), initialfile=safe + '-合并记录.md', defaultextension='.md', filetypes=[('Markdown', '*.md')])
        if not destination:
            return
        self.export_button.configure(state='disabled')
        self.status.set('正在查找并合并所有日志片段…')
        home = self.home
        def run():
            try:
                files = matching_files(home, row['id'], row.get('rollout_path', ''))
                if Path(destination).resolve() in {p.resolve() for p in files}:
                    raise ValueError('不能覆盖原始日志文件。')
                records, warnings = collect_messages(files)
                if not records:
                    raise ValueError('没有找到可导出的用户或助手消息。')
                text = render_markdown(title, row['id'], files, records, warnings)
                Path(destination).write_text(text, encoding='utf-8')
                counts = collections.Counter(r['role'] for r in records)
                report = f'已合并 {len(files)} 个片段，导出 {counts["user"]} 条用户消息、{counts["assistant"]} 条助手消息。'
                if warnings:
                    report += f' 有 {len(warnings)} 行无法解析，请查看文末提示。'
                self.root.after(0, lambda: self.finished(report, destination, bool(warnings)))
            except Exception as exc:
                self.root.after(0, lambda message=str(exc): self.failed(message))
        threading.Thread(target=run, daemon=True).start()

    def finished(self, report, path, warning):
        self.export_button.configure(state='normal')
        self.status.set(report)
        (messagebox.showwarning if warning else messagebox.showinfo)('导出完成', report + '\n\n保存位置：\n' + path)

    def failed(self, message):
        self.export_button.configure(state='normal')
        self.status.set('导出失败')
        messagebox.showerror('导出失败', message)


if __name__ == '__main__':
    window = tk.Tk()
    ExportApp(window)
    window.mainloop()
