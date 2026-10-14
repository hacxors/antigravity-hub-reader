# -*- coding: utf-8 -*-
"""
✦ Antigravity Hub - Leitor de Conversas & Apoio à Continuidade ✦
"""

import hashlib
import json
import mimetypes
import os
import platform
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

HOST = "127.0.0.1"
PORT = 8765

# Arquivo para armazenar favoritos, tags e notas pessoais do usuário
APP_DIR = Path(__file__).parent.resolve()
USER_DATA_FILE = APP_DIR / "antigravity_ideas_and_notes.json"

# Cache em memória para rotas rápidas
CONVERSATIONS_INDEX = {}

MIME_TABLE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".ts": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def load_user_data():
    if USER_DATA_FILE.exists():
        try:
            with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"favorites": {}, "notes": {}, "tags": {}}


def save_user_data(data):
    try:
        with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def get_search_roots():
    home = Path.home()
    roots = [
        home / ".gemini" / "antigravity-ide",
        home / ".gemini" / "antigravity",
        home / ".gemini" / "antigravity-cli",
        home / ".antigravity-ide",
        home / ".antigravity",
        home / ".gemini",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.extend([
            Path(appdata) / "Antigravity IDE",
            Path(appdata) / "Antigravity",
            Path(appdata) / "Google" / "Antigravity",
        ])
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.extend([
            Path(local_appdata) / "antigravity-ide",
            Path(local_appdata) / "antigravity",
        ])
    if platform.system() == "Darwin":
        app_support = home / "Library" / "Application Support"
        roots.extend([
            app_support / "Antigravity IDE",
            app_support / "Antigravity",
        ])
    xdg_config = os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))
    roots.extend([
        Path(xdg_config) / "antigravity-ide",
        Path(xdg_config) / "antigravity",
    ])
    valid = []
    for r in roots:
        try:
            resolved = r.resolve()
            if resolved.exists() and resolved not in valid:
                valid.append(resolved)
        except Exception:
            continue
    return valid


def clean_user_prompt(raw_text):
    if not raw_text:
        return ""
    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw_text, re.DOTALL | re.IGNORECASE)
    if m:
        clean = m.group(1).strip()
    else:
        clean = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", raw_text, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", clean, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", "", clean).strip()
    return clean or raw_text.strip()


def extract_workspace_from_text(raw_text):
    if not raw_text:
        return ""
    m = re.search(r"Active Document:\s*([a-zA-Z]:\\[^\r\n]+)", raw_text)
    if m:
        return str(Path(m.group(1).strip()).parent)
    m = re.search(r"workspace:\s*([a-zA-Z]:\\[^\r\n]+)", raw_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"\[URI\]\s*->\s*\[CorpusName\]:\s*\n\s*([^\s\r\n]+)", raw_text)
    if m:
        clean = unquote(m.group(1).replace("file://", "")).rstrip("/\\")
        if re.match(r"^/[a-zA-Z]:", clean):
            clean = clean[1:]
        return clean
    return ""


def open_readonly_sqlite(path):
    p_str = str(path.resolve()).replace("\\", "/")
    uri = f"file:{quote(p_str, safe='/:')}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=5)


def extract_project_info_from_db(con):
    project_name = ""
    project_path = ""
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_metadata_blob'")
        if cur.fetchone():
            row = cur.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
            if row and row[0]:
                blob = row[0]
                decoded = blob.decode("utf-8", errors="ignore")
                m = re.search(r"file://([^\s\x00-\x1f\"\'<>]+)", decoded)
                if m:
                    clean = unquote(m.group(1)).rstrip("/\\")
                    if re.match(r"^/[a-zA-Z]:", clean):
                        clean = clean[1:]
                    p = Path(clean)
                    return p.name or clean, str(p)
    except Exception:
        pass
    return project_name, project_path


def parse_transcript_file(transcript_path: Path):
    turns = []
    tools_used = set()
    files_touched = set()
    commands_run = []
    user_requests = []
    model_conclusions = []
    workspace_candidate = ""

    if not transcript_path.exists():
        return turns, tools_used, files_touched, commands_run, user_requests, model_conclusions, workspace_candidate

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line_idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                source = obj.get("source", "")
                stype = obj.get("type", "")
                raw_content = obj.get("content", "")
                created_at = obj.get("created_at", "")
                step_idx = obj.get("step_index", line_idx)
                tool_calls = obj.get("tool_calls", [])

                if not workspace_candidate and raw_content:
                    ws = extract_workspace_from_text(raw_content)
                    if ws:
                        workspace_candidate = ws

                if stype == "USER_INPUT" or source == "USER_EXPLICIT":
                    clean_req = clean_user_prompt(raw_content)
                    if clean_req:
                        user_requests.append(clean_req)
                        turns.append({
                            "role": "user",
                            "step": step_idx,
                            "type": "USER_INPUT",
                            "text": clean_req,
                            "raw_text": raw_content,
                            "time": created_at,
                        })

                elif stype == "PLANNER_RESPONSE" or source == "MODEL":
                    clean_text = raw_content.strip() if raw_content else ""
                    extracted_tools = []

                    if tool_calls and isinstance(tool_calls, list):
                        for tc in tool_calls:
                            tname = tc.get("name", "tool")
                            tools_used.add(tname)
                            args = tc.get("args", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    pass

                            summary = ""
                            detail = ""
                            if isinstance(args, dict):
                                summary = args.get("toolSummary") or args.get("toolAction") or ""
                                if tname in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
                                    target = args.get("TargetFile") or args.get("target_file") or ""
                                    if target:
                                        files_touched.add(str(target))
                                        detail = str(target)
                                elif tname == "run_command":
                                    cmd = args.get("CommandLine") or args.get("command") or ""
                                    if cmd:
                                        commands_run.append(str(cmd))
                                        detail = str(cmd)
                                elif tname in ("view_file", "read_file"):
                                    fpath = args.get("AbsolutePath") or args.get("path") or ""
                                    if fpath:
                                        detail = str(fpath)

                            extracted_tools.append({
                                "name": tname,
                                "summary": summary,
                                "detail": detail,
                                "args": args,
                            })

                    if stype in ("RUN_COMMAND", "CODE_ACTION", "WRITE_TO_FILE", "REPLACE_FILE_CONTENT", "VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH", "BROWSER_SUBAGENT"):
                        tools_used.add(stype.lower())
                        if not clean_text and not extracted_tools:
                            continue

                    if clean_text or extracted_tools:
                        if clean_text:
                            model_conclusions.append(clean_text)
                        turns.append({
                            "role": "assistant",
                            "step": step_idx,
                            "type": stype,
                            "text": clean_text,
                            "tool_calls": extracted_tools,
                            "time": created_at,
                        })

                elif stype in ("CODE_ACTION", "RUN_COMMAND", "WRITE_TO_FILE", "REPLACE_FILE_CONTENT", "VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH"):
                    tools_used.add(stype.lower())

    except Exception:
        pass

    return turns, tools_used, files_touched, commands_run, user_requests, model_conclusions, workspace_candidate


def scan_all_conversations(extra_dir=None):
    roots = get_search_roots()
    if extra_dir and Path(extra_dir).exists():
        roots.insert(0, Path(extra_dir).resolve())

    conv_map = {}

    for root in roots:
        brain_dir = root / "brain"
        if not brain_dir.exists():
            brain_dir = root if root.name == "brain" else None
        if brain_dir and brain_dir.is_dir():
            try:
                for b in brain_dir.iterdir():
                    if b.is_dir() and len(b.name) >= 10:
                        cid = b.name
                        if cid not in conv_map:
                            conv_map[cid] = {
                                "uuid": cid,
                                "brain_path": b,
                                "db_path": None,
                                "mtime": b.stat().st_mtime,
                            }
                        else:
                            if b.stat().st_mtime > conv_map[cid]["mtime"]:
                                conv_map[cid]["brain_path"] = b
                                conv_map[cid]["mtime"] = b.stat().st_mtime
            except Exception:
                pass

        convs_dir = root / "conversations"
        if not convs_dir.exists():
            convs_dir = root if root.name == "conversations" else None
        if convs_dir and convs_dir.is_dir():
            try:
                for ext in ("*.db", "*.sqlite", "*.sqlite3", "*.pb"):
                    for f in convs_dir.glob(ext):
                        cid = f.stem
                        mtime = f.stat().st_mtime
                        if cid not in conv_map:
                            conv_map[cid] = {
                                "uuid": cid,
                                "brain_path": None,
                                "db_path": f if f.suffix != ".pb" else None,
                                "mtime": mtime,
                            }
                        else:
                            if f.suffix != ".pb" and not conv_map[cid]["db_path"]:
                                conv_map[cid]["db_path"] = f
                            if mtime > conv_map[cid]["mtime"]:
                                conv_map[cid]["mtime"] = mtime
            except Exception:
                pass

    user_data = load_user_data()
    favorites = user_data.get("favorites", {})
    notes = user_data.get("notes", {})
    tags = user_data.get("tags", {})

    items = []
    for cid, info in conv_map.items():
        db_id = hashlib.sha1(cid.encode("utf-8")).hexdigest()[:12]
        CONVERSATIONS_INDEX[db_id] = info

        mtime = info["mtime"]
        dt_str = datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")

        title = cid[:24]
        project = "Sem Projeto / Geral"
        project_path = ""
        prompt_count = 0
        response_count = 0
        tools_summary = []
        artifacts_count = 0

        b_path = info.get("brain_path")
        if b_path and b_path.exists():
            artifacts = [f.name for f in b_path.iterdir() if f.name != ".system_generated" and not f.name.startswith(".")]
            artifacts_count = len(artifacts)

            tfile = b_path / ".system_generated" / "logs" / "transcript.jsonl"
            if not tfile.exists():
                tfile = b_path / ".system_generated" / "logs" / "overview.txt"
            if not tfile.exists():
                tfile = b_path / "transcript.jsonl"

            if tfile.exists():
                try:
                    with open(tfile, "r", encoding="utf-8", errors="replace") as tf:
                        for line in tf:
                            try:
                                o = json.loads(line)
                                st = o.get("type")
                                src = o.get("source")
                                if st == "USER_INPUT" or src == "USER_EXPLICIT":
                                    prompt_count += 1
                                    if title == cid[:24]:
                                        p = clean_user_prompt(o.get("content", ""))
                                        if p:
                                            title = p[:100].replace("\n", " ").strip()
                                elif st == "PLANNER_RESPONSE" or src == "MODEL":
                                    if o.get("content"):
                                        response_count += 1
                                    for tc in o.get("tool_calls", []):
                                        tn = tc.get("name")
                                        if tn and tn not in tools_summary:
                                            tools_summary.append(tn)
                            except Exception:
                                pass
                except Exception:
                    pass

        if info.get("db_path") and info["db_path"].exists():
            try:
                with open_readonly_sqlite(info["db_path"]) as con:
                    pname, ppath = extract_project_info_from_db(con)
                    if pname:
                        project = pname
                        project_path = ppath
            except Exception:
                pass

        if project == "Sem Projeto / Geral" and project_path:
            project = Path(project_path).name or project_path

        items.append({
            "id": db_id,
            "uuid": cid,
            "title": title,
            "project": project,
            "project_path": project_path,
            "modified": mtime,
            "date_str": dt_str,
            "prompts_count": prompt_count,
            "responses_count": response_count,
            "tools_count": len(tools_summary),
            "tools_sample": tools_summary[:4],
            "artifacts_count": artifacts_count,
            "is_favorite": bool(favorites.get(cid)),
            "note": notes.get(cid, ""),
            "tags": tags.get(cid, []),
        })

    items.sort(key=lambda x: (not x["is_favorite"], -x["modified"]))
    return items


def get_conversation_full(db_id: str):
    info = CONVERSATIONS_INDEX.get(db_id)
    if not info:
        return {"error": "Conversa não encontrada."}

    cid = info["uuid"]
    b_path = info.get("brain_path")
    db_path = info.get("db_path")

    project = "Sem Projeto / Geral"
    project_path = ""

    if db_path and db_path.exists():
        try:
            with open_readonly_sqlite(db_path) as con:
                pname, ppath = extract_project_info_from_db(con)
                if pname:
                    project = pname
                    project_path = ppath
        except Exception:
            pass

    transcript_file = None
    if b_path and b_path.exists():
        candidates = [
            b_path / ".system_generated" / "logs" / "transcript_full.jsonl",
            b_path / ".system_generated" / "logs" / "transcript.jsonl",
            b_path / ".system_generated" / "logs" / "overview.txt",
            b_path / "transcript_full.jsonl",
            b_path / "transcript.jsonl",
        ]
        for c in candidates:
            if c.exists() and c.is_file() and c.stat().st_size > 0:
                transcript_file = c
                break

    turns = []
    tools_used = set()
    files_touched = set()
    commands_run = []
    user_requests = []
    model_conclusions = []
    workspace_candidate = ""

    if transcript_file:
        (
            turns,
            tools_used,
            files_touched,
            commands_run,
            user_requests,
            model_conclusions,
            workspace_candidate,
        ) = parse_transcript_file(transcript_file)

    if not project_path and workspace_candidate:
        project_path = workspace_candidate
        project = Path(workspace_candidate).name or workspace_candidate

    artifacts_list = []
    if b_path and b_path.exists():
        for f in b_path.iterdir():
            if f.name == ".system_generated" or f.name.startswith("."):
                continue
            is_file = f.is_file()
            size = f.stat().st_size if is_file else 0
            ext = f.suffix.lower()
            mime = MIME_TABLE.get(ext) or mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            is_img = ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".ico")
            artifacts_list.append({
                "name": f.name,
                "is_dir": f.is_dir(),
                "size": size,
                "mime": mime,
                "is_image": is_img,
                "is_md": ext == ".md",
            })

    first_request = user_requests[0] if user_requests else "Desenvolvimento da aplicação."
    last_conclusion = model_conclusions[-1] if model_conclusions else "Sessão concluída ou interrompida."
    clean_last_conclusion = re.sub(r"```[\s\S]*?```", "[Bloco de Código]", last_conclusion)[:600].strip()

    continuity_prompt = (
        f"### CONTEXTO DA SESSÃO ANTERIOR (Antigravity)\n"
        f"- **Projeto / Workspace**: `{project_path or project}`\n"
        f"- **Objetivo Original**: {first_request[:350]}\n\n"
        f"### O QUE FOI FEITO ATÉ AGORA\n"
    )
    if files_touched:
        continuity_prompt += "- **Arquivos Tocados / Criados**:\n"
        for ft in sorted(files_touched)[:12]:
            continuity_prompt += f"  • `{ft}`\n"
    if commands_run:
        continuity_prompt += "- **Comandos Principais Executados**:\n"
        for cmd in commands_run[:6]:
            continuity_prompt += f"  • `{cmd}`\n"
    continuity_prompt += f"- **Último Status / Onde Paramos**:\n  {clean_last_conclusion}\n\n"
    continuity_prompt += (
        f"### NOVA INSTRUÇÃO DE DESENVOLVIMENTO\n"
        f"Com base no contexto acima do projeto `{project}`, continue exatamente a partir de onde parou:\n"
        f"1. Analise o estado atual dos arquivos modificados.\n"
        f"2. Prossiga com a implementação dos próximos passos pendentes para concluir o objetivo.\n"
    )

    user_data = load_user_data()
    cid = info["uuid"]

    return {
        "id": db_id,
        "uuid": cid,
        "title": user_requests[0][:100].strip() if user_requests else cid[:24],
        "project": project,
        "project_path": project_path,
        "date_str": datetime.fromtimestamp(info["mtime"]).strftime("%d/%m/%Y %H:%M"),
        "turns": turns,
        "tools_used": sorted(list(tools_used)),
        "files_touched": sorted(list(files_touched)),
        "commands_run": commands_run,
        "artifacts": artifacts_list,
        "first_request": first_request,
        "last_conclusion": last_conclusion,
        "continuity_prompt": continuity_prompt,
        "is_favorite": bool(user_data.get("favorites", {}).get(cid)),
        "note": user_data.get("notes", {}).get(cid, ""),
        "tags": user_data.get("tags", {}).get(cid, []),
    }


HTML_APP = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>✦ Antigravity Hub | Leitor & Apoio à Continuidade</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<!-- Marked para Markdown de alta performance -->
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<!-- Highlight.js para realce de sintaxe de códigos -->
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark-dimmed.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
:root {
  --bg-dark: #0a0e14;
  --bg-sidebar: #10141d;
  --bg-card: #161c28;
  --bg-card-hover: #1e2636;
  --bg-card-active: #1d2c47;
  --border: #242e42;
  --border-focus: #38bdf8;
  --text-main: #f1f5f9;
  --text-muted: #94a3b8;
  --text-dim: #64748b;
  --accent-blue: #38bdf8;
  --accent-cyan: #06b6d4;
  --accent-indigo: #818cf8;
  --accent-purple: #c084fc;
  --accent-green: #10b981;
  --accent-amber: #f59e0b;
  --accent-rose: #f43f5e;
  --code-bg: #090d14;
  --user-bg: #111a2c;
  --user-border: #1d3356;
  --ai-bg: #121824;
  --ai-border: #202b3d;
  --tool-bg: #0e121a;
  --tool-border: #202636;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg-dark);
  color: var(--text-main);
  display: flex;
  height: 100vh;
  overflow: hidden;
  font-size: 14px;
}
::-webkit-scrollbar { width: 7px; height: 7px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #222d40; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #344460; }

/* SIDEBAR */
#sidebar {
  width: 410px;
  min-width: 330px;
  max-width: 500px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100vh;
  user-select: none;
}
.sidebar-top {
  padding: 16px 16px 12px;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, rgba(22,28,40,0.85) 0%, rgba(16,20,29,0.98) 100%);
}
.brand-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.brand-logo {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  font-size: 15.5px;
  letter-spacing: -0.3px;
  color: var(--text-main);
}
.brand-badge {
  background: linear-gradient(135deg, #0ea5e9, #6366f1);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 6px;
  text-transform: uppercase;
}
.brand-actions { display: flex; gap: 6px; }
.icon-btn {
  background: var(--bg-card);
  border: 1px solid var(--border);
  color: var(--text-muted);
  width: 30px;
  height: 30px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.15s ease;
  font-size: 13px;
}
.icon-btn:hover {
  background: var(--bg-card-hover);
  color: var(--text-main);
  border-color: var(--border-focus);
}
.search-wrapper { position: relative; margin-bottom: 10px; }
.search-wrapper input {
  width: 100%;
  padding: 8px 12px 8px 32px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-main);
  font-size: 13px;
  outline: none;
  transition: 0.15s ease;
}
.search-wrapper input:focus {
  border-color: var(--accent-blue);
  box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.15);
}
.search-icon {
  position: absolute;
  left: 10px;
  top: 9px;
  color: var(--text-dim);
  font-size: 13px;
}
.filter-bar { display: flex; gap: 6px; align-items: center; }
.filter-select {
  flex: 1;
  padding: 6px 10px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-main);
  font-size: 12px;
  outline: none;
  cursor: pointer;
}
.filter-btn-pill {
  padding: 5px 10px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-muted);
  font-size: 11.5px;
  cursor: pointer;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 4px;
  transition: all 0.15s ease;
}
.filter-btn-pill.active {
  background: #1e3352;
  color: var(--accent-blue);
  border-color: var(--accent-blue);
  font-weight: 600;
}
.stats-summary {
  padding: 6px 16px;
  font-size: 11px;
  color: var(--text-dim);
  display: flex;
  justify-content: space-between;
  border-bottom: 1px solid rgba(255,255,255,0.03);
  background: #0d1219;
}
.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.conv-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 11px 13px;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.conv-card:hover {
  background: var(--bg-card-hover);
  border-color: #3b5075;
  transform: translateY(-1px);
}
.conv-card.active {
  background: var(--bg-card-active);
  border-color: var(--accent-blue);
  box-shadow: 0 4px 14px rgba(6, 182, 212, 0.12);
}
.conv-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.conv-title {
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--text-main);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.conv-star {
  color: #64748b;
  font-size: 15px;
  cursor: pointer;
  padding: 2px 4px;
  transition: transform 0.15s ease;
}
.conv-star.starred { color: var(--accent-amber); }
.conv-star:hover { transform: scale(1.25); }
.conv-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-muted);
}
.project-tag {
  background: #172438;
  color: var(--accent-blue);
  border: 1px solid #233b5d;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 10.5px;
  font-weight: 500;
  max-width: 170px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-badges { display: flex; gap: 6px; align-items: center; }
.badge-pill {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #1a2233;
  color: var(--text-dim);
  border: 1px solid rgba(255,255,255,0.06);
}
.badge-has-artifacts {
  color: var(--accent-green);
  border-color: rgba(16, 185, 129, 0.25);
  background: rgba(16, 185, 129, 0.1);
}
.badge-has-note {
  color: var(--accent-amber);
  border-color: rgba(245, 158, 11, 0.25);
  background: rgba(245, 158, 11, 0.1);
}

/* MAIN PANEL */
#main-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg-dark);
  position: relative;
  overflow: hidden;
}
.main-header {
  padding: 14px 22px;
  background: var(--bg-sidebar);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  z-index: 10;
}
.header-info { flex: 1; min-width: 0; }
.header-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text-main);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.header-sub {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 3px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.header-actions { display: flex; gap: 8px; align-items: center; }
.btn {
  padding: 7px 14px;
  border-radius: 7px;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.15s ease;
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--text-main);
  white-space: nowrap;
}
.btn:hover {
  background: var(--bg-card-hover);
  border-color: var(--border-focus);
}
.btn-primary {
  background: linear-gradient(135deg, #0284c7, #2563eb);
  border-color: #38bdf8;
  color: #fff;
  font-weight: 600;
  box-shadow: 0 2px 10px rgba(37, 99, 235, 0.25);
}
.btn-primary:hover {
  background: linear-gradient(135deg, #0369a1, #1d4ed8);
  box-shadow: 0 4px 14px rgba(37, 99, 235, 0.4);
}

/* NAV TABS */
.nav-tabs {
  display: flex;
  gap: 2px;
  padding: 0 20px;
  background: #0d121b;
  border-bottom: 1px solid var(--border);
}
.tab-btn {
  padding: 10px 16px;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text-muted);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 7px;
  transition: all 0.15s ease;
}
.tab-btn:hover { color: var(--text-main); }
.tab-btn.active {
  color: var(--accent-blue);
  border-bottom-color: var(--accent-blue);
  font-weight: 600;
}
.tab-badge {
  background: #1c2738;
  color: var(--text-muted);
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
}
.tab-btn.active .tab-badge {
  background: #1e3a66;
  color: var(--accent-blue);
}

/* TAB PANES */
.tab-content { flex: 1; overflow: hidden; display: flex; flex-direction: column; }
.tab-pane {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  display: none;
}
.tab-pane.active {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* BARRA DE FILTROS & BUSCA DENTRO DE DIÁLOGO & RACIOCÍNIO */
.chat-filter-box {
  background: #101622;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}
.chat-search-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.chat-search-field {
  position: relative;
  flex: 1;
}
.chat-search-icon {
  position: absolute;
  left: 10px;
  top: 8px;
  font-size: 12.5px;
  color: var(--text-dim);
}
.chat-search-input {
  width: 100%;
  padding: 7px 30px 7px 32px;
  background: #161d2b;
  border: 1px solid #28354c;
  border-radius: 6px;
  color: var(--text-main);
  font-size: 13px;
  outline: none;
  transition: border-color 0.15s ease;
}
.chat-search-input:focus {
  border-color: var(--accent-blue);
}
.clear-search-btn {
  position: absolute;
  right: 8px;
  top: 7px;
  background: transparent;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 12px;
}
.clear-search-btn:hover { color: var(--text-main); }
.chat-bulk-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}
.chat-filter-pills-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 4px;
  border-top: 1px solid rgba(255,255,255,0.04);
}
.pill-group {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.chat-filter-pill {
  padding: 4px 10px;
  background: #151c2a;
  border: 1px solid #28374f;
  border-radius: 6px;
  color: var(--text-muted);
  font-size: 11.5px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.chat-filter-pill:hover {
  background: #1d273a;
  color: var(--text-main);
}
.chat-filter-pill.active {
  background: #1e3557;
  border-color: var(--accent-blue);
  color: var(--accent-blue);
  font-weight: 600;
}
.chat-filter-extra {
  display: flex;
  align-items: center;
  gap: 12px;
}
.switch-label {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-muted);
}

/* BOLHAS DE MENSAGENS COM EXPANSÃO/RECOLHIMENTO */
.msg-bubble {
  border-radius: 10px;
  border: 1px solid var(--border);
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 98%;
  box-shadow: 0 3px 12px rgba(0,0,0,0.25);
  transition: all 0.2s ease;
  animation: fadeIn 0.15s ease;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(3px); }
  to { opacity: 1; transform: translateY(0); }
}
.msg-user {
  background: var(--user-bg);
  border-color: var(--user-border);
  width: 95%;
  align-self: flex-start;
}
.msg-assistant {
  background: var(--ai-bg);
  border-color: var(--ai-border);
  width: 99%;
  align-self: flex-start;
}
.msg-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  padding-bottom: 7px;
}
.msg-author-tag {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 700;
  letter-spacing: 0.3px;
}
.msg-user .msg-author-tag { color: var(--accent-blue); }
.msg-assistant .msg-author-tag { color: var(--accent-purple); }
.msg-actions { display: flex; gap: 6px; align-items: center; }

.btn-micro {
  background: transparent;
  border: 1px solid rgba(255,255,255,0.1);
  color: var(--text-muted);
  font-size: 11px;
  padding: 3px 9px;
  border-radius: 4px;
  cursor: pointer;
  transition: 0.15s ease;
}
.btn-micro:hover {
  background: rgba(255,255,255,0.08);
  color: var(--text-main);
  border-color: var(--accent-blue);
}

/* ESTADO RECOLHIDO (PADRÃO: MÁXIMO ~3 LINHAS) */
.msg-collapsed .msg-body {
  max-height: 74px;
  overflow: hidden;
  position: relative;
  cursor: pointer;
  mask-image: linear-gradient(180deg, #000 45%, transparent 100%);
  -webkit-mask-image: linear-gradient(180deg, #000 45%, transparent 100%);
}
.msg-collapsed .tools-accordion {
  display: none !important;
}

/* ESTADO EXPANDIDO */
.msg-expanded .msg-body {
  max-height: none !important;
  overflow: visible;
  mask-image: none !important;
  -webkit-mask-image: none !important;
}
.msg-expanded .tools-accordion {
  display: flex !important;
}

/* RODAPÉ DO BLOCO PARA EXPANDIR */
.msg-expand-footer {
  padding-top: 6px;
  display: flex;
  justify-content: center;
  align-items: center;
  border-top: 1px dashed rgba(255,255,255,0.06);
  margin-top: 4px;
  cursor: pointer;
}
.msg-expand-btn {
  background: transparent;
  border: none;
  color: var(--accent-blue);
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 6px;
  transition: all 0.15s ease;
}
.msg-expand-btn:hover {
  background: rgba(56, 189, 248, 0.12);
  color: #7dd3fc;
}

/* REALCE DE TERMOS BUSCADOS */
mark.chat-hl {
  background: rgba(245, 158, 11, 0.35);
  color: #fef08a;
  padding: 0 3px;
  border-radius: 3px;
  font-weight: 600;
}

/* FORMATAÇÃO RICA DO CORPO DO TEXTO (MARKDOWN) */
.msg-body {
  font-size: 14.5px;
  line-height: 1.72;
  color: #e2e8f0;
  word-break: break-word;
  transition: max-height 0.2s ease;
}
.msg-body p { margin-bottom: 12px; }
.msg-body p:last-child { margin-bottom: 0; }
.msg-body h1, .msg-body h2, .msg-body h3, .msg-body h4 {
  color: #f8fafc;
  font-weight: 600;
  margin: 16px 0 8px 0;
  letter-spacing: -0.2px;
}
.msg-body h1 { font-size: 18px; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 6px; }
.msg-body h2 { font-size: 16px; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 4px; }
.msg-body h3 { font-size: 14.5px; color: var(--accent-blue); }
.msg-body h4 { font-size: 13.5px; color: var(--accent-indigo); }

.msg-body ul, .msg-body ol { margin-left: 22px; margin-bottom: 12px; }
.msg-body li { margin-bottom: 4px; }
.msg-body blockquote {
  border-left: 3px solid var(--accent-blue);
  background: rgba(56, 189, 248, 0.05);
  padding: 10px 14px;
  border-radius: 0 6px 6px 0;
  margin: 12px 0;
  color: #cbd5e1;
  font-style: italic;
}
.msg-body table {
  width: 100%;
  border-collapse: collapse;
  margin: 14px 0;
  font-size: 13px;
  background: #0f141d;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.msg-body th {
  background: #172030;
  color: var(--accent-blue);
  font-weight: 600;
  text-align: left;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
}
.msg-body td {
  padding: 8px 12px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  color: #cbd5e1;
}
.msg-body tr:last-child td { border-bottom: none; }
.msg-body tr:hover td { background: rgba(255,255,255,0.02); }
.msg-body code:not(pre code) {
  font-family: 'Fira Code', monospace;
  background: #172030;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12.5px;
  color: #38bdf8;
  border: 1px solid rgba(255,255,255,0.06);
}

/* BLOCOS DE CÓDIGO */
.code-block-wrapper {
  margin: 12px 0;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #27354d;
  background: var(--code-bg);
}
.code-header {
  background: #131b29;
  padding: 6px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #243147;
  font-size: 11.5px;
  color: var(--text-muted);
  font-family: 'Fira Code', monospace;
}
pre code {
  display: block;
  padding: 12px 14px;
  overflow-x: auto;
  font-family: 'Fira Code', monospace;
  font-size: 13px;
  line-height: 1.55;
  background: transparent !important;
  color: #f1f5f9;
}

/* FERRAMENTAS */
.tools-accordion {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tool-item {
  background: var(--tool-bg);
  border: 1px solid var(--tool-border);
  border-radius: 6px;
  overflow: hidden;
  font-size: 12px;
}
.tool-summary-header {
  padding: 7px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  background: rgba(255,255,255,0.02);
}
.tool-summary-header:hover { background: rgba(255,255,255,0.04); }
.tool-badge {
  font-weight: 600;
  color: var(--accent-indigo);
  font-family: 'Fira Code', monospace;
}
.tool-action-desc {
  color: var(--text-muted);
  margin-left: 8px;
  font-size: 11.5px;
}
.tool-detail-content {
  padding: 10px 14px;
  background: #090c13;
  border-top: 1px solid var(--tool-border);
  font-family: 'Fira Code', monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  display: none;
  max-height: 300px;
  overflow-y: auto;
}

/* PAINEL DE CONTINUIDADE */
.continuity-container {
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-width: 900px;
  margin: 0 auto;
  width: 100%;
}
.continuity-card {
  background: linear-gradient(180deg, #151d2c 0%, #101622 100%);
  border: 1px solid #2b3952;
  border-radius: 12px;
  padding: 22px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.3);
}
.continuity-card h3 {
  font-size: 16px;
  color: var(--accent-blue);
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.continuity-desc {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.5;
  margin-bottom: 18px;
}
.continuity-textarea {
  width: 100%;
  height: 280px;
  background: #0b0f17;
  border: 1px solid #293850;
  border-radius: 8px;
  padding: 14px;
  color: #e2e8f0;
  font-family: 'Fira Code', monospace;
  font-size: 13px;
  line-height: 1.6;
  resize: vertical;
  outline: none;
}
.continuity-textarea:focus { border-color: var(--accent-blue); }
.continuity-action-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 14px;
  gap: 12px;
  flex-wrap: wrap;
}
.template-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
.template-btn {
  background: #172233;
  border: 1px solid #283952;
  color: var(--text-muted);
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  cursor: pointer;
  transition: 0.15s ease;
}
.template-btn:hover {
  background: #20314a;
  color: var(--accent-blue);
  border-color: var(--accent-blue);
}

/* CARDS DE ARTEFATOS & IMAGENS */
.grid-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}
.item-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: all 0.15s ease;
  cursor: pointer;
}
.item-card:hover {
  background: var(--bg-card-hover);
  border-color: var(--border-focus);
}
.item-card-preview {
  width: 100%;
  height: 130px;
  border-radius: 6px;
  background: #0a0d13;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border: 1px solid rgba(255,255,255,0.06);
}
.item-card-preview img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
.item-card-title {
  font-weight: 600;
  font-size: 13px;
  word-break: break-all;
  display: flex;
  align-items: center;
  gap: 6px;
}
.item-card-meta { font-size: 11.5px; color: var(--text-dim); }

/* MODAL DE VISUALIZAÇÃO DE ARTEFATOS & IMAGENS */
.artifact-preview-modal {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.85);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 999;
  padding: 24px;
  backdrop-filter: blur(4px);
}
.artifact-modal-box {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  width: 95%;
  max-width: 1000px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 16px 48px rgba(0,0,0,0.7);
}
.modal-header {
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--bg-sidebar);
}
.modal-body {
  padding: 20px;
  overflow-y: auto;
  flex: 1;
}

/* VISUALIZADOR DE IMAGEM */
.image-viewer-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 100%;
  align-items: center;
}
.image-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding: 8px 12px;
  background: #111722;
  border-radius: 8px;
  border: 1px solid var(--border);
}
.image-info-tag {
  font-size: 12px;
  color: var(--text-muted);
  font-family: 'Fira Code', monospace;
}
.image-display-box {
  width: 100%;
  max-height: 72vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: radial-gradient(#1f293d 1px, #0b0e14 1px);
  background-size: 16px 16px;
  border-radius: 8px;
  border: 1px solid var(--border);
  padding: 20px;
  overflow: auto;
}
.modal-img-preview {
  max-width: 100%;
  max-height: 68vh;
  object-fit: contain;
  border-radius: 6px;
  box-shadow: 0 8px 30px rgba(0,0,0,0.5);
  border: 1px solid rgba(255,255,255,0.1);
}

/* NOTAS */
.notes-container {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 800px;
  margin: 0 auto;
  width: 100%;
}
.note-textarea {
  width: 100%;
  height: 200px;
  background: #0b0f17;
  border: 1px solid #2b3952;
  border-radius: 8px;
  padding: 14px;
  color: #f1f5f9;
  font-size: 13.5px;
  line-height: 1.6;
  outline: none;
}
.note-textarea:focus { border-color: var(--accent-amber); }

.empty-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-muted);
  text-align: center;
  gap: 14px;
  padding: 30px;
}
.empty-icon { font-size: 44px; opacity: 0.6; }

@media(max-width: 920px) {
  body { flex-direction: column; }
  #sidebar { width: 100%; height: 42vh; max-width: none; }
  #main-panel { height: 58vh; }
}
</style>
</head>
<body>

<!-- SIDEBAR -->
<aside id="sidebar">
  <div class="sidebar-top">
    <div class="brand-title">
      <div class="brand-logo">
        <span style="font-size:18px;">✦</span>
        <span>Antigravity Hub</span>
        <span class="brand-badge">IDE Apoio</span>
      </div>
      <div class="brand-actions">
        <button class="icon-btn" onclick="loadConversations()" title="Atualizar lista">🔄</button>
      </div>
    </div>
    
    <div class="search-wrapper">
      <span class="search-icon">🔍</span>
      <input id="search-input" placeholder="Buscar em conversas, ideias ou projetos...">
    </div>

    <div class="filter-bar">
      <select id="project-select" class="filter-select">
        <option value="">📁 Todos os Projetos</option>
      </select>
      <button id="filter-starred" class="filter-btn-pill" onclick="toggleStarredFilter()">
        <span>★</span> Favoritos
      </button>
    </div>
  </div>

  <div class="stats-summary" id="stats-summary">
    <span>Buscando conversas...</span>
    <span id="total-count-badge">0 sessões</span>
  </div>

  <div class="conv-list" id="conv-list">
    <div class="empty-placeholder">
      <div class="empty-icon">📂</div>
      <div>Carregando histórico do Antigravity...</div>
    </div>
  </div>
</aside>

<!-- MAIN CONTENT -->
<main id="main-panel">
  <!-- Top Bar -->
  <header class="main-header">
    <div class="header-info">
      <h2 class="header-title" id="active-title">Selecione uma conversa</h2>
      <div class="header-sub" id="active-sub">Nenhuma sessão selecionada</div>
    </div>
    <div class="header-actions">
      <button class="btn btn-primary" id="btn-quick-continue" onclick="switchTab('continuity')" title="Gerar instrução para nova sessão do Antigravity">
        <span>🚀</span> Continuar Desenvolvimento
      </button>
      <button class="btn" id="btn-copy-markdown" onclick="copyFullMarkdown()">
        <span>📋</span> Copiar Markdown
      </button>
      <button class="btn" id="btn-toggle-favorite" onclick="toggleActiveFavorite()">
        <span id="fav-icon">☆</span> Favoritar
      </button>
    </div>
  </header>

  <!-- Navigation Tabs -->
  <nav class="nav-tabs">
    <button class="tab-btn active" onclick="switchTab('chat')" id="tab-btn-chat">
      <span>💬</span> Diálogo & Raciocínio
      <span class="tab-badge" id="badge-turns">0</span>
    </button>
    <button class="tab-btn" onclick="switchTab('continuity')" id="tab-btn-continuity">
      <span>🚀</span> Instrução de Continuidade
      <span class="tab-badge" style="color:var(--accent-blue);">Novo</span>
    </button>
    <button class="tab-btn" onclick="switchTab('code')" id="tab-btn-code">
      <span>📁</span> Arquivos & Comandos
      <span class="tab-badge" id="badge-files">0</span>
    </button>
    <button class="tab-btn" onclick="switchTab('artifacts')" id="tab-btn-artifacts">
      <span>📄</span> Artefatos & Mídias
      <span class="tab-badge" id="badge-artifacts">0</span>
    </button>
    <button class="tab-btn" onclick="switchTab('notes')" id="tab-btn-notes">
      <span>💡</span> Notas da Ideia
      <span class="tab-badge" id="badge-notes">0</span>
    </button>
  </nav>

  <!-- Tab Contents -->
  <div class="tab-content">
    
    <!-- PANE 1: CHAT COM FILTROS AVANÇADOS E EXPANSÃO/RECOLHIMENTO -->
    <div class="tab-pane active" id="pane-chat">
      <div class="chat-filter-box">
        <div class="chat-search-row">
          <div class="chat-search-field">
            <span class="chat-search-icon">🔍</span>
            <input class="chat-search-input" id="chat-search-input" placeholder="Filtrar conteúdos no diálogo (palavra, função, erro, comando)..." oninput="applyChatFilters()">
            <button class="clear-search-btn" id="chat-clear-search-btn" onclick="clearChatSearch()" title="Limpar busca" style="display:none;">✕</button>
          </div>
          <div class="chat-bulk-actions">
            <button class="btn-micro" onclick="expandAllTurns(true)" title="Expandir todos os blocos">📂 Expandir Todos</button>
            <button class="btn-micro" onclick="expandAllTurns(false)" title="Recolher todos os blocos para 3 linhas">📁 Recolher Todos</button>
          </div>
        </div>
        <div class="chat-filter-pills-row">
          <div class="pill-group">
            <button class="chat-filter-pill active" id="chat-pill-all" onclick="setChatFilterRole('all')">Todos (<span id="count-all">0</span>)</button>
            <button class="chat-filter-pill" id="chat-pill-user" onclick="setChatFilterRole('user')">👤 Prompts (<span id="count-user">0</span>)</button>
            <button class="chat-filter-pill" id="chat-pill-assistant" onclick="setChatFilterRole('assistant')">✦ Respostas (<span id="count-assistant">0</span>)</button>
            <button class="chat-filter-pill" id="chat-pill-code" onclick="setChatFilterRole('code')">💻 Com Código (<span id="count-code">0</span>)</button>
            <button class="chat-filter-pill" id="chat-pill-tools" onclick="setChatFilterRole('tools')">⚙ Com Ferramentas (<span id="count-tools">0</span>)</button>
          </div>
          <div class="chat-filter-extra">
            <label class="switch-label">
              <input type="checkbox" id="toggle-technical" onchange="renderChatTurns()" checked>
              <span>Exibir Ferramentas</span>
            </label>
            <button class="btn-micro" onclick="copyCleanPrompts()">Copiar Prompts</button>
          </div>
        </div>
      </div>

      <div id="messages-container" style="display:flex; flex-direction:column; gap:14px;">
        <div class="empty-placeholder">
          <div class="empty-icon">💡</div>
          <div>Escolha uma sessão na barra lateral para explorar as discussões e ideias.</div>
        </div>
      </div>
    </div>

    <!-- PANE 2: CONTINUITY GENERATOR -->
    <div class="tab-pane" id="pane-continuity">
      <div class="continuity-container">
        <div class="continuity-card">
          <h3><span>🚀</span> Gerador de Instrução para Nova Sessão</h3>
          <p class="continuity-desc">
            Use este gerador para transferir o contexto e ideias desta sessão diretamente para uma nova conversa do Antigravity. Ele sintetiza o objetivo original, os arquivos alterados e o ponto onde você parou, preparando uma instrução pronta para dar continuidade imediata.
          </p>

          <div class="template-buttons">
            <span style="font-size:11.5px; color:var(--text-dim); align-self:center;">Templates Rápidos:</span>
            <button class="template-btn" onclick="applyTemplate('continue')">▶ Continuar de onde parou</button>
            <button class="template-btn" onclick="applyTemplate('refactor')">🛠 Revisar & Corrigir Bugs</button>
            <button class="template-btn" onclick="applyTemplate('test_deploy')">📦 Testar & Finalizar Módulo</button>
            <button class="template-btn" onclick="applyTemplate('explain')">🔍 Explicar Arquitetura</button>
          </div>

          <div class="continuity-prompt-area" style="margin-top:14px;">
            <textarea id="continuity-textarea" class="continuity-textarea" placeholder="Gerando prompt de continuidade..."></textarea>
          </div>

          <div class="continuity-action-bar">
            <span style="font-size:12px; color:var(--text-muted);">
              Dica: Cole este texto diretamente no campo de prompt do Antigravity para iniciar a próxima sessão sem perda de contexto.
            </span>
            <button class="btn btn-primary" onclick="copyContinuityPrompt()">
              <span>📋</span> Copiar Instrução de Continuidade
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- PANE 3: CODE & FILES -->
    <div class="tab-pane" id="pane-code">
      <div style="margin-bottom:12px;">
        <h3 style="font-size:15px; margin-bottom:6px;">Arquivos Modificados na Sessão</h3>
        <p style="font-size:12.5px; color:var(--text-muted);">Lista de arquivos criados ou editados pelo Antigravity durante esta conversa:</p>
      </div>
      <div class="grid-cards" id="files-grid">
        <div class="empty-placeholder">Nenhum arquivo detectado.</div>
      </div>

      <div style="margin-top:24px; margin-bottom:12px;">
        <h3 style="font-size:15px; margin-bottom:6px;">Comandos Executados no Terminal</h3>
        <p style="font-size:12.5px; color:var(--text-muted);">Comandos rodados no ambiente via PowerShell / Bash:</p>
      </div>
      <div id="commands-list" style="display:flex; flex-direction:column; gap:8px;">
        <div class="empty-placeholder">Nenhum comando executado.</div>
      </div>
    </div>

    <!-- PANE 4: ARTIFACTS -->
    <div class="tab-pane" id="pane-artifacts">
      <div style="margin-bottom:14px;">
        <h3 style="font-size:15px; margin-bottom:4px;">Artefatos Gerados pelo Agente</h3>
        <p style="font-size:12.5px; color:var(--text-muted);">Arquivos markdown, planos de tarefas, walkthroughs e imagens capturadas na pasta da conversa:</p>
      </div>
      <div class="grid-cards" id="artifacts-grid">
        <div class="empty-placeholder">Nenhum artefato salvo nesta sessão.</div>
      </div>
    </div>

    <!-- PANE 5: NOTES & IDEAS -->
    <div class="tab-pane" id="pane-notes">
      <div class="notes-container">
        <div class="continuity-card">
          <h3><span>💡</span> Bloco de Ideias & Notas Pessoais</h3>
          <p class="continuity-desc">
            Anote aqui suas ideias, planos de arquitetura, referências ou anotações sobre o que planeja desenvolver a seguir. Suas notas são salvas localmente e associadas a esta conversa específica.
          </p>
          <textarea id="note-input" class="note-textarea" placeholder="Ex: Ideia para a próxima versão: integrar com banco de dados MySQL, adicionar tela de ranking de jogadores e configurar webhook..."></textarea>
          <div style="display:flex; justify-content:flex-end; margin-top:12px;">
            <button class="btn btn-primary" onclick="saveActiveNote()">
              <span>💾</span> Salvar Anotações
            </button>
          </div>
        </div>
      </div>
    </div>

  </div>
</main>

<!-- ARTIFACT VIEWER MODAL -->
<div class="artifact-preview-modal" id="artifact-modal" onclick="closeArtifactModal(event)">
  <div class="artifact-modal-box" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3 id="modal-filename" style="font-size:14px; font-weight:600;">Artefato</h3>
      <div style="display:flex; gap:8px; align-items:center;">
        <button class="btn-micro" id="modal-copy-btn" onclick="copyModalContent()">Copiar Conteúdo</button>
        <button class="icon-btn" onclick="closeArtifactModal()">✕</button>
      </div>
    </div>
    <div class="modal-body" id="modal-content">
      Carregando...
    </div>
  </div>
</div>

<script>
let allConversations = [];
let activeId = null;
let currentChat = null;
let filterStarredOnly = false;
let modalRawContent = '';

// ESTADOS DE FILTRO E EXPANSÃO DO DIÁLOGO
let chatFilterRole = 'all'; // 'all' | 'user' | 'assistant' | 'code' | 'tools'
let chatSearchQuery = '';
let expandedTurns = new Set(); // Inicia vazio: TODOS os turnos começam RECOLHIDOS por padrão!

const $ = id => document.getElementById(id);
const esc = s => String(s || '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

if (typeof marked !== 'undefined') {
  marked.setOptions({
    gfm: true,
    breaks: true
  });
}

async function loadConversations() {
  try {
    $('stats-summary').firstElementChild.textContent = 'Buscando conversas...';
    const res = await fetch('/api/list');
    allConversations = await res.json();
    populateProjects();
    renderConversationList();
    $('stats-summary').firstElementChild.textContent = 'Histórico sincronizado';
    $('total-count-badge').textContent = allConversations.length + ' sessões';

    if (!activeId && allConversations.length > 0) {
      openConversation(allConversations[0].id);
    }
  } catch (err) {
    $('conv-list').innerHTML = '<div class="empty-placeholder">Erro ao carregar lista de conversas.</div>';
  }
}

function populateProjects() {
  const projs = {};
  allConversations.forEach(c => {
    const p = c.project || 'Sem Projeto / Geral';
    projs[p] = (projs[p] || 0) + 1;
  });
  const sel = $('project-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="">📁 Todos os Projetos (' + allConversations.length + ')</option>';
  Object.keys(projs).sort().forEach(p => {
    const opt = document.createElement('option');
    opt.value = p;
    opt.textContent = `📁 ${p} (${projs[p]})`;
    if (p === cur) opt.selected = true;
    sel.appendChild(opt);
  });
}

function toggleStarredFilter() {
  filterStarredOnly = !filterStarredOnly;
  $('filter-starred').classList.toggle('active', filterStarredOnly);
  renderConversationList();
}

function renderConversationList() {
  const q = $('search-input').value.toLowerCase().trim();
  const sp = $('project-select').value;

  const filtered = allConversations.filter(c => {
    if (filterStarredOnly && !c.is_favorite) return false;
    if (sp && c.project !== sp) return false;
    if (!q) return true;
    const hay = ((c.title || '') + ' ' + (c.project || '') + ' ' + (c.note || '') + ' ' + c.uuid).toLowerCase();
    return hay.includes(q);
  });

  if (!filtered.length) {
    $('conv-list').innerHTML = '<div class="empty-placeholder"><div class="empty-icon">🔍</div><div>Nenhuma conversa encontrada.</div></div>';
    return;
  }

  $('conv-list').innerHTML = filtered.map(c => `
    <div class="conv-card ${activeId === c.id ? 'active' : ''}" onclick="openConversation('${c.id}')">
      <div class="conv-card-header">
        <div class="conv-title">${esc(c.title || c.uuid)}</div>
        <span class="conv-star ${c.is_favorite ? 'starred' : ''}" onclick="event.stopPropagation(); toggleStar('${c.uuid}', ${!c.is_favorite})" title="${c.is_favorite ? 'Desfavoritar' : 'Favoritar'}">
          ${c.is_favorite ? '★' : '☆'}
        </span>
      </div>
      <div class="conv-meta">
        <span class="project-tag" title="${esc(c.project)}">${esc(c.project)}</span>
        <span>${c.date_str}</span>
      </div>
      <div class="card-badges">
        <span class="badge-pill">${c.prompts_count} prompts</span>
        ${c.tools_count > 0 ? `<span class="badge-pill">${c.tools_count} ferramentas</span>` : ''}
        ${c.artifacts_count > 0 ? `<span class="badge-pill badge-has-artifacts">${c.artifacts_count} artefatos</span>` : ''}
        ${c.note ? `<span class="badge-pill badge-has-note">💡 com nota</span>` : ''}
      </div>
    </div>
  `).join('');
}

async function openConversation(id) {
  activeId = id;
  renderConversationList();
  $('messages-container').innerHTML = '<div class="empty-placeholder"><div class="empty-icon">⏳</div><div>Carregando histórico detalhado...</div></div>';

  // Reseta filtros e estados ao abrir uma nova conversa
  expandedTurns.clear(); // Por padrão, tudo vem recolhido!
  chatFilterRole = 'all';
  chatSearchQuery = '';
  if ($('chat-search-input')) $('chat-search-input').value = '';
  if ($('chat-clear-search-btn')) $('chat-clear-search-btn').style.display = 'none';

  try {
    const res = await fetch('/api/read?id=' + encodeURIComponent(id));
    currentChat = await res.json();
    if (currentChat.error) {
      $('messages-container').innerHTML = `<div class="empty-placeholder">${esc(currentChat.error)}</div>`;
      return;
    }

    $('active-title').textContent = currentChat.title || currentChat.uuid;
    $('active-sub').innerHTML = `
      <b style="color:var(--accent-blue);">${esc(currentChat.project)}</b> · 
      ${currentChat.date_str} · 
      ID: <code>${currentChat.uuid.slice(0, 14)}...</code>
      ${currentChat.project_path ? `· <span title="${esc(currentChat.project_path)}">📁 ${esc(currentChat.project_path)}</span>` : ''}
    `;

    $('fav-icon').textContent = currentChat.is_favorite ? '★' : '☆';
    $('badge-turns').textContent = currentChat.turns ? currentChat.turns.length : 0;
    $('badge-files').textContent = (currentChat.files_touched ? currentChat.files_touched.length : 0) + (currentChat.commands_run ? currentChat.commands_run.length : 0);
    $('badge-artifacts').textContent = currentChat.artifacts ? currentChat.artifacts.length : 0;
    $('badge-notes').textContent = currentChat.note ? '1' : '0';

    $('continuity-textarea').value = currentChat.continuity_prompt || '';
    $('note-input').value = currentChat.note || '';

    updateChatFilterCounts();
    renderChatTurns();
    renderFilesAndCommands();
    renderArtifacts();
  } catch (err) {
    $('messages-container').innerHTML = '<div class="empty-placeholder">Erro ao carregar dados da conversa.</div>';
  }
}

function updateChatFilterCounts() {
  if (!currentChat || !currentChat.turns) {
    ['all', 'user', 'assistant', 'code', 'tools'].forEach(k => {
      const el = $('count-' + k);
      if (el) el.textContent = '0';
    });
    return;
  }

  const turns = currentChat.turns;
  const countAll = turns.length;
  let countUser = 0;
  let countAssistant = 0;
  let countCode = 0;
  let countTools = 0;

  turns.forEach(t => {
    if (t.role === 'user') countUser++;
    else countAssistant++;

    const text = t.text || '';
    if (text.includes('```') || text.includes('<code>') || (t.tool_calls && t.tool_calls.some(tc => ['write_to_file', 'replace_file_content', 'code_action'].includes(tc.name)))) {
      countCode++;
    }

    if (t.tool_calls && t.tool_calls.length > 0) {
      countTools++;
    }
  });

  if ($('count-all')) $('count-all').textContent = countAll;
  if ($('count-user')) $('count-user').textContent = countUser;
  if ($('count-assistant')) $('count-assistant').textContent = countAssistant;
  if ($('count-code')) $('count-code').textContent = countCode;
  if ($('count-tools')) $('count-tools').textContent = countTools;

  document.querySelectorAll('.chat-filter-pill').forEach(p => p.classList.remove('active'));
  const activePill = $('chat-pill-' + chatFilterRole);
  if (activePill) activePill.classList.add('active');
}

function setChatFilterRole(role) {
  chatFilterRole = role;
  document.querySelectorAll('.chat-filter-pill').forEach(p => p.classList.remove('active'));
  const el = $('chat-pill-' + role);
  if (el) el.classList.add('active');
  renderChatTurns();
}

function applyChatFilters() {
  chatSearchQuery = ($('chat-search-input').value || '').trim();
  $('chat-clear-search-btn').style.display = chatSearchQuery ? 'block' : 'none';

  // Se houver busca ativa, expande automaticamente os turnos encontrados
  if (chatSearchQuery && currentChat && currentChat.turns) {
    const q = chatSearchQuery.toLowerCase();
    currentChat.turns.forEach((t, i) => {
      const match = (t.text || '').toLowerCase().includes(q) || (t.tool_calls && JSON.stringify(t.tool_calls).toLowerCase().includes(q));
      if (match) expandedTurns.add(i);
    });
  }

  renderChatTurns();
}

function clearChatSearch() {
  $('chat-search-input').value = '';
  $('chat-clear-search-btn').style.display = 'none';
  chatSearchQuery = '';
  renderChatTurns();
}

function toggleTurnExpand(idx) {
  if (expandedTurns.has(idx)) {
    expandedTurns.delete(idx);
  } else {
    expandedTurns.add(idx);
  }

  const bubble = $(`turn-bubble-${idx}`);
  if (bubble) {
    const isExp = expandedTurns.has(idx);
    bubble.classList.toggle('msg-expanded', isExp);
    bubble.classList.toggle('msg-collapsed', !isExp);
    const btnHeader = $(`expand-btn-header-${idx}`);
    const btnFooter = $(`expand-btn-footer-${idx}`);
    if (btnHeader) btnHeader.textContent = isExp ? '▴ Recolher' : '▾ Expandir';
    if (btnFooter) btnFooter.innerHTML = isExp ? '▴ Recolher turno' : '▾ Expandir turno completo';
  }
}

function expandAllTurns(expand) {
  if (!currentChat || !currentChat.turns) return;
  if (expand) {
    currentChat.turns.forEach((_, i) => expandedTurns.add(i));
  } else {
    expandedTurns.clear();
  }
  renderChatTurns();
}

function renderChatTurns() {
  if (!currentChat || !currentChat.turns || !currentChat.turns.length) {
    $('messages-container').innerHTML = '<div class="empty-placeholder"><div class="empty-icon">💬</div><div>Nenhuma mensagem registrada nesta conversa.</div></div>';
    return;
  }

  const showTech = $('toggle-technical').checked;
  const q = chatSearchQuery.toLowerCase();
  let matchedCount = 0;
  let html = '';

  currentChat.turns.forEach((t, i) => {
    const isUser = t.role === 'user';
    const hasTools = t.tool_calls && t.tool_calls.length > 0;
    const text = t.text || '';
    const hasCode = text.includes('```') || text.includes('<code>') || (hasTools && t.tool_calls.some(tc => ['write_to_file', 'replace_file_content', 'code_action'].includes(tc.name)));

    if (!isUser && !text && !hasTools) return;
    if (!showTech && !isUser && !text) return;

    // Filtro por papel (role/tipo)
    if (chatFilterRole === 'user' && !isUser) return;
    if (chatFilterRole === 'assistant' && isUser) return;
    if (chatFilterRole === 'code' && !hasCode) return;
    if (chatFilterRole === 'tools' && !hasTools) return;

    // Filtro por texto de busca interna
    if (q) {
      const matchText = text.toLowerCase().includes(q);
      const matchTools = hasTools && JSON.stringify(t.tool_calls).toLowerCase().includes(q);
      if (!matchText && !matchTools) return;
    }

    matchedCount++;
    const isExpanded = expandedTurns.has(i);
    const lineCount = (text.split('\n').length || 1);
    const isShort = lineCount <= 2 && text.length < 130 && !hasCode && !hasTools;

    // Renderiza com destaque se houver busca
    let renderedContent = renderRichMarkdown(text);
    if (q) {
      const safeQ = esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const reg = new RegExp(`(${safeQ})`, 'gi');
      renderedContent = renderedContent.replace(reg, '<mark class="chat-hl">$1</mark>');
    }

    html += `
      <div class="msg-bubble ${isUser ? 'msg-user' : 'msg-assistant'} ${isExpanded ? 'msg-expanded' : 'msg-collapsed'}" id="turn-bubble-${i}">
        <div class="msg-header">
          <div class="msg-author-tag">
            <span>${isUser ? '👤 PROMPT DO USUÁRIO' : '✦ ANTIGRAVITY IDE'}</span>
            ${t.time ? `<span style="font-size:11px; font-weight:400; color:var(--text-dim);">${t.time.replace('T', ' ').slice(0, 19)}</span>` : ''}
          </div>
          <div class="msg-actions">
            ${!isShort ? `
              <button class="btn-micro" id="expand-btn-header-${i}" onclick="toggleTurnExpand(${i})">
                ${isExpanded ? '▴ Recolher' : '▾ Expandir'}
              </button>
            ` : ''}
            <button class="btn-micro" onclick="copyTurnText(${i}, this)">Copiar</button>
          </div>
        </div>

        <div class="msg-body" onclick="if(!expandedTurns.has(${i})){ toggleTurnExpand(${i}); }">
          ${renderedContent}
        </div>

        ${(showTech && hasTools) ? `
          <div class="tools-accordion">
            ${t.tool_calls.map((tool, ti) => `
              <div class="tool-item">
                <div class="tool-summary-header" onclick="toggleToolDetail('tool-${i}-${ti}')">
                  <div>
                    <span class="tool-badge">⚙ ${esc(tool.name)}</span>
                    <span class="tool-action-desc">${esc(tool.summary || tool.detail || '')}</span>
                  </div>
                  <span style="font-size:10px; color:var(--text-dim);">expandir ▾</span>
                </div>
                <div class="tool-detail-content" id="tool-${i}-${ti}">
                  ${esc(JSON.stringify(tool.args, null, 2))}
                </div>
              </div>
            `).join('')}
          </div>
        ` : ''}

        ${!isShort ? `
          <div class="msg-expand-footer" onclick="toggleTurnExpand(${i})">
            <span class="msg-expand-btn" id="expand-btn-footer-${i}">
              ${isExpanded ? '▴ Recolher turno' : '▾ Expandir turno completo'}
            </span>
          </div>
        ` : ''}
      </div>
    `;
  });

  if (matchedCount === 0) {
    html = `
      <div class="empty-placeholder">
        <div class="empty-icon">🔍</div>
        <div>Nenhum turno encontrado com os filtros selecionados.</div>
        <button class="btn btn-micro" onclick="clearChatSearch(); setChatFilterRole('all');" style="margin-top:8px;">Limpar Filtros</button>
      </div>
    `;
  }

  $('messages-container').innerHTML = html;
}

function toggleToolDetail(id) {
  const el = $(id);
  if (el) {
    el.style.display = el.style.display === 'block' ? 'none' : 'block';
  }
}

function renderFilesAndCommands() {
  if (!currentChat) return;

  const files = currentChat.files_touched || [];
  if (files.length) {
    $('files-grid').innerHTML = files.map(f => `
      <div class="item-card">
        <div class="item-card-title">📄 ${esc(f.split(/[\\/]/).pop())}</div>
        <div class="item-card-meta" style="font-family:'Fira Code', monospace; word-break:break-all;">${esc(f)}</div>
        <button class="btn-micro" style="align-self:flex-start; margin-top:4px;" onclick="copyToClipboard('${esc(f)}')">Copiar Caminho</button>
      </div>
    `).join('');
  } else {
    $('files-grid').innerHTML = '<div class="empty-placeholder">Nenhum arquivo modificado detectado.</div>';
  }

  const cmds = currentChat.commands_run || [];
  if (cmds.length) {
    $('commands-list').innerHTML = cmds.map(c => `
      <div class="tool-item" style="padding:10px 14px; background:#0e131d; display:flex; justify-content:space-between; align-items:center;">
        <code style="font-family:'Fira Code', monospace; font-size:12.5px; color:#38bdf8;">$ ${esc(c)}</code>
        <button class="btn-micro" onclick="copyToClipboard('${esc(c)}')">Copiar</button>
      </div>
    `).join('');
  } else {
    $('commands-list').innerHTML = '<div class="empty-placeholder">Nenhum comando de terminal registrado.</div>';
  }
}

function renderArtifacts() {
  if (!currentChat) return;
  const list = currentChat.artifacts || [];
  if (!list.length) {
    $('artifacts-grid').innerHTML = '<div class="empty-placeholder">Nenhum artefato encontrado na pasta da conversa.</div>';
    return;
  }

  $('artifacts-grid').innerHTML = list.map(a => {
    const ext = (a.name.split('.').pop() || '').toLowerCase();
    const isImg = a.is_image || ['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp', 'ico'].includes(ext);
    const imgUrl = `/api/artifact?id=${encodeURIComponent(currentChat.uuid)}&file=${encodeURIComponent(a.name)}`;

    return `
      <div class="item-card" onclick="openArtifact('${esc(a.name)}')">
        ${isImg ? `
          <div class="item-card-preview">
            <img src="${imgUrl}" alt="${esc(a.name)}" loading="lazy" onerror="this.style.display='none'">
          </div>
        ` : ''}
        <div class="item-card-title">
          <span>${isImg ? '🖼' : (a.is_md ? '📝' : '📄')}</span>
          <span>${esc(a.name)}</span>
        </div>
        <div class="item-card-meta">${(a.size / 1024).toFixed(1)} KB · ${esc(a.mime)}</div>
        <button class="btn-micro" style="align-self:flex-start; margin-top:2px;">Visualizar</button>
      </div>
    `;
  }).join('');
}

async function openArtifact(filename) {
  if (!currentChat) return;
  $('modal-filename').textContent = filename;
  $('artifact-modal').style.display = 'flex';
  $('modal-content').innerHTML = '<div class="empty-placeholder">Carregando conteúdo...</div>';

  const ext = (filename.split('.').pop() || '').toLowerCase();
  const isImage = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp', 'ico'].includes(ext);
  const url = `/api/artifact?id=${encodeURIComponent(currentChat.uuid)}&file=${encodeURIComponent(filename)}`;

  if (isImage) {
    $('modal-copy-btn').style.display = 'none';
    $('modal-content').innerHTML = `
      <div class="image-viewer-container">
        <div class="image-toolbar">
          <span class="image-info-tag">🖼 Imagem: ${esc(filename)}</span>
          <div style="display:flex; gap:8px;">
            <a href="${url}" target="_blank" class="btn btn-micro">🔗 Abrir em Nova Aba</a>
            <a href="${url}" download="${esc(filename)}" class="btn btn-micro">📥 Baixar Imagem</a>
          </div>
        </div>
        <div class="image-display-box">
          <img src="${url}" alt="${esc(filename)}" class="modal-img-preview" onerror="this.parentElement.innerHTML='<div style=\\'color:#f87171; padding:20px;\\'>Erro ao renderizar imagem.</div>'" />
        </div>
      </div>
    `;
    return;
  }

  $('modal-copy-btn').style.display = 'inline-block';
  try {
    const res = await fetch(url);
    if (!res.ok) {
      $('modal-content').innerHTML = `<div class="empty-placeholder">Erro ao carregar arquivo (Status ${res.status}).</div>`;
      return;
    }
    const text = await res.text();
    modalRawContent = text;
    if (ext === 'md') {
      $('modal-content').innerHTML = `<div class="msg-body">${renderRichMarkdown(text)}</div>`;
    } else {
      $('modal-content').innerHTML = `
        <div class="code-block-wrapper">
          <div class="code-header">
            <span>${ext.toUpperCase() || 'ARQUIVO'}</span>
            <button class="btn-micro" onclick="copyToClipboard(modalRawContent, 'Copiado!', this)">Copiar Código</button>
          </div>
          <pre><code>${esc(text)}</code></pre>
        </div>
      `;
    }
  } catch (err) {
    $('modal-content').innerHTML = '<div class="empty-placeholder">Erro ao carregar o arquivo.</div>';
  }
}

function copyModalContent() {
  if (modalRawContent) {
    copyToClipboard(modalRawContent, '✓ Conteúdo Copiado!');
  }
}

function closeArtifactModal() {
  $('artifact-modal').style.display = 'none';
  modalRawContent = '';
}

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));

  const btn = $('tab-btn-' + name);
  const pane = $('pane-' + name);
  if (btn) btn.classList.add('active');
  if (pane) pane.classList.add('active');
}

function applyTemplate(type) {
  if (!currentChat) return;
  const project = currentChat.project_path || currentChat.project;
  const orig = currentChat.first_request || 'Desenvolvimento';
  const last = (currentChat.last_conclusion || '').replace(/```[\s\S]*?```/g, '[código]').slice(0, 400).trim();

  let text = `### CONTEXTO DE CONTINUIDADE (Antigravity)\n- **Projeto**: \`${project}\`\n- **Objetivo Original**: ${orig}\n- **Último Estado**: ${last}\n\n`;

  if (type === 'continue') {
    text += `### INSTRUÇÃO\nPor favor, continue exatamente de onde parou no projeto \`${currentChat.project}\`:\n1. Revise os arquivos tocados na sessão anterior.\n2. Implemente o próximo passo da lógica que ficou pendente e confirme os resultados.\n`;
  } else if (type === 'refactor') {
    text += `### INSTRUÇÃO DE REFATORAÇÃO & CORREÇÃO\nPor favor, inspecione os arquivos criados recentemente no projeto \`${currentChat.project}\`:\n1. Verifique possíveis inconsistências, erros de sintaxe ou imports quebrados.\n2. Refatore para melhorar a clareza, modularidade e performance.\n`;
  } else if (type === 'test_deploy') {
    text += `### INSTRUÇÃO DE TESTES & FINALIZAÇÃO\nCom base no código implementado até aqui:\n1. Crie testes ou validações para certificar que todas as funcionalidades estão operando corretamente.\n2. Prepare os passos finais para execução e entrega do módulo.\n`;
  } else if (type === 'explain') {
    text += `### INSTRUÇÃO DE ANÁLISE DE ARQUITETURA\nFaça um resumo executivo da arquitetura que construímos até o momento neste projeto:\n- Quais foram os componentes criados?\n- Como eles interagem entre si?\n- Quais são as próximas melhorias recomendadas?\n`;
  }
  $('continuity-textarea').value = text;
}

function copyContinuityPrompt() {
  const val = $('continuity-textarea').value;
  copyToClipboard(val, '✓ Instrução de Continuidade Copiada!');
}

function copyFullMarkdown() {
  if (!currentChat || !currentChat.turns) return;
  const md = currentChat.turns.map(t => {
    const header = t.role === 'user' ? '## 👤 Prompt do Usuário' : '## ✦ Retorno Antigravity';
    return `${header}\n\n${t.text || ''}\n`;
  }).join('\n---\n\n');
  copyToClipboard(md, '✓ Conversa Completa em Markdown Copiada!');
}

function copyCleanPrompts() {
  if (!currentChat || !currentChat.turns) return;
  const prompts = currentChat.turns
    .filter(t => t.role === 'user')
    .map((t, idx) => `### Prompt ${idx + 1}\n${t.text}`)
    .join('\n\n---\n\n');
  copyToClipboard(prompts, '✓ Prompts do Usuário Copiados!');
}

function copyTurnText(idx, btn) {
  if (!currentChat || !currentChat.turns[idx]) return;
  copyToClipboard(currentChat.turns[idx].text, 'Copiado!', btn);
}

function copyToClipboard(text, msg = 'Copiado!', targetBtn = null) {
  navigator.clipboard.writeText(text).then(() => {
    if (targetBtn) {
      const orig = targetBtn.textContent;
      targetBtn.textContent = msg;
      setTimeout(() => { targetBtn.textContent = orig; }, 1600);
    } else {
      alert(msg);
    }
  });
}

async function toggleStar(uuid, newState) {
  try {
    await fetch('/api/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uuid, is_favorite: newState })
    });
    const item = allConversations.find(c => c.uuid === uuid);
    if (item) item.is_favorite = newState;
    if (currentChat && currentChat.uuid === uuid) {
      currentChat.is_favorite = newState;
      $('fav-icon').textContent = newState ? '★' : '☆';
    }
    renderConversationList();
  } catch (e) {}
}

function toggleActiveFavorite() {
  if (!currentChat) return;
  toggleStar(currentChat.uuid, !currentChat.is_favorite);
}

async function saveActiveNote() {
  if (!currentChat) return;
  const note = $('note-input').value.trim();
  try {
    await fetch('/api/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uuid: currentChat.uuid, note })
    });
    currentChat.note = note;
    const item = allConversations.find(c => c.uuid === currentChat.uuid);
    if (item) item.note = note;
    $('badge-notes').textContent = note ? '1' : '0';
    renderConversationList();
    alert('✓ Anotação salva com sucesso!');
  } catch (e) {
    alert('Erro ao salvar anotação.');
  }
}

// RENDERIZADOR RICO DE TEXTO & MARKDOWN
function renderRichMarkdown(rawText) {
  if (!rawText) return '';

  if (typeof marked !== 'undefined') {
    try {
      const html = marked.parse(rawText);
      const tempDiv = document.createElement('div');
      tempDiv.innerHTML = html;

      tempDiv.querySelectorAll('pre').forEach(pre => {
        const codeEl = pre.querySelector('code');
        const codeText = codeEl ? codeEl.innerText : pre.innerText;
        const className = codeEl ? codeEl.className : '';
        const langMatch = className.match(/language-([a-zA-Z0-9_\-]+)/);
        const lang = langMatch ? langMatch[1] : 'CÓDIGO';

        const wrapper = document.createElement('div');
        wrapper.className = 'code-block-wrapper';

        const header = document.createElement('div');
        header.className = 'code-header';
        header.innerHTML = `
          <span>${lang.toUpperCase()}</span>
          <button class="btn-micro" onclick="copyToClipboard(decodeURIComponent('${encodeURIComponent(codeText)}'), 'Copiado!', this)">Copiar Código</button>
        `;

        wrapper.appendChild(header);
        pre.parentNode.insertBefore(wrapper, pre);
        wrapper.appendChild(pre);

        if (typeof hljs !== 'undefined' && codeEl) {
          hljs.highlightElement(codeEl);
        }
      });

      return tempDiv.innerHTML;
    } catch (e) {}
  }

  // Fallback nativo
  let text = esc(rawText);

  text = text.replace(/```([a-zA-Z0-9_\-\+]*)\n([\s\S]*?)```/g, (match, lang, code) => {
    const l = lang ? lang.trim() : 'CÓDIGO';
    return `
      <div class="code-block-wrapper">
        <div class="code-header">
          <span>${l.toUpperCase()}</span>
          <button class="btn-micro" onclick="copyToClipboard(decodeURIComponent('${encodeURIComponent(code.trim())}'), 'Copiado!', this)">Copiar Código</button>
        </div>
        <pre><code>${code.trim()}</code></pre>
      </div>
    `;
  });

  text = text.replace(/^#### (.*$)/gim, '<h4>$1</h4>');
  text = text.replace(/^### (.*$)/gim, '<h3>$1</h3>');
  text = text.replace(/^## (.*$)/gim, '<h2>$1</h2>');
  text = text.replace(/^# (.*$)/gim, '<h1>$1</h1>');

  text = text.replace(/\*\*(.*?)\*\*/g, '<b>$1</b>');
  text = text.replace(/\*(.*?)\*/g, '<i>$1</i>');
  text = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" style="color:var(--accent-blue); text-decoration:underline;">$1</a>');

  text = text.replace(/^\s*[\-\*]\s+(.*$)/gim, '<li>$1</li>');
  text = text.replace(/(<li>.*<\/li>)/gis, '<ul>$1</ul>');

  text = text.replace(/\n\n+/g, '</p><p>');
  text = text.replace(/\n/g, '<br>');
  text = '<p>' + text + '</p>';
  text = text.replace(/<p><\/p>/g, '');

  return text;
}

$('search-input').addEventListener('input', renderConversationList);
$('project-select').addEventListener('change', renderConversationList);

loadConversations();
</script>
</body>
</html>"""


class AntigravityAppHandler(BaseHTTPRequestHandler):
    def send_json(self, payload, code=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            return self.send_html(HTML_APP)

        if path == "/api/list":
            items = scan_all_conversations()
            return self.send_json(items)

        if path == "/api/read":
            query = parse_qs(parsed.query)
            conv_id = query.get("id", [""])[0]
            data = get_conversation_full(conv_id)
            code = 404 if "error" in data else 200
            return self.send_json(data, code=code)

        if path == "/api/artifact":
            query = parse_qs(parsed.query)
            conv_uuid = query.get("id", [""])[0]
            filename = query.get("file", [""])[0]

            if not conv_uuid or not filename:
                self.send_error(400, "Parâmetros inválidos.")
                return

            target_path = None
            roots = get_search_roots()

            for root in roots:
                candidate = root / "brain" / conv_uuid / filename
                if candidate.exists() and candidate.is_file():
                    target_path = candidate
                    break

            if not target_path:
                for root in roots:
                    b_folder = root / "brain" / conv_uuid
                    if b_folder.exists() and b_folder.is_dir():
                        matches = list(b_folder.rglob(filename))
                        if matches and matches[0].is_file():
                            target_path = matches[0]
                            break

            if not target_path or not target_path.exists():
                self.send_error(404, "Arquivo não encontrado.")
                return

            ext = target_path.suffix.lower()
            mime = MIME_TABLE.get(ext) or mimetypes.guess_type(str(target_path))[0] or "application/octet-stream"

            try:
                content = target_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(content)
                return
            except Exception as e:
                self.send_error(500, f"Erro ao ler arquivo: {e}")
                return

        self.send_error(404, "Não encontrado.")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/notes":
            content_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_len).decode("utf-8")
            try:
                body = json.loads(raw_body)
                uuid = body.get("uuid")
                if not uuid:
                    return self.send_json({"error": "UUID ausente."}, code=400)

                user_data = load_user_data()
                if "is_favorite" in body:
                    if body["is_favorite"]:
                        user_data["favorites"][uuid] = True
                    else:
                        user_data["favorites"].pop(uuid, None)

                if "note" in body:
                    note_text = body["note"].strip()
                    if note_text:
                        user_data["notes"][uuid] = note_text
                    else:
                        user_data["notes"].pop(uuid, None)

                save_user_data(user_data)
                return self.send_json({"status": "ok"})
            except Exception as e:
                return self.send_json({"error": str(e)}, code=500)

        self.send_error(404)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 64)
    print("✦ Antigravity Hub - Leitor de Conversas & Suporte à Continuidade ✦")
    print("=" * 64)

    roots = get_search_roots()
    found_brains = 0
    found_dbs = 0
    for r in roots:
        b = r / "brain"
        if b.exists():
            found_brains += len([d for d in b.iterdir() if d.is_dir()])
        c = r / "conversations"
        if c.exists():
            found_dbs += len(list(c.glob("*.db")))

    print(f"  [OK] Diretórios mapeados: {len(roots)}")
    print(f"  [OK] Sessões em Brain: {found_brains}")
    print(f"  [OK] Sessões em SQLite/PB: {found_dbs}")
    print("-" * 64)
    print(f"  🚀 Servidor rodando em: http://{HOST}:{PORT}")
    print("  Pressione Ctrl+C para encerrar o servidor.")
    print("=" * 64)

    threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()

    try:
        server = ThreadingHTTPServer((HOST, PORT), AntigravityAppHandler)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado pelo usuário.")
        sys.exit(0)