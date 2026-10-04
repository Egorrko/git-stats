#!/usr/bin/env python3
"""Локальный дашборд статистики git-репозитория.

    python3 gitstats.py [путь-к-репо] [--port 8765] [--roots ~/my,~/Work] [--no-open]

Поднимает сервер на 127.0.0.1 и открывает браузер. Сервер только читает
историю (`git log --numstat`); фильтры по автору, периоду и файлам считает
страница, кнопка «Обновить» заново читает git.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
REC, FLD = "\x1e", "\x1f"
LOG_FORMAT = (
    "%x1e%H%x1f%P%x1f%aN%x1f%aE%x1f%aI%x1f%s"
    "%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x1d)"
)
SKIP_DIRS = {
    "node_modules", "deps", "_build", "target", "vendor", "venv",
    "__pycache__", "dist", "build", "site-packages",
}
RENAME_BRACES = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")


class GitError(Exception):
    pass


def git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", repo, "-c", "core.quotepath=off", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {args[0]} завершился с ошибкой")
    return proc.stdout


def repo_root(path):
    path = os.path.expanduser(path or "")
    if not os.path.isdir(path):
        raise GitError(f"Нет такой папки: {path}")
    return git(path, "rev-parse", "--show-toplevel").strip()


def branches(repo):
    out = git(repo, "for-each-ref", "--sort=-committerdate",
              "--format=%(refname:short)", "refs/heads", "refs/remotes")
    return [b for b in out.splitlines() if b and not b.endswith("/HEAD")]


def all_refs(repo):
    return set(git(repo, "for-each-ref", "--format=%(refname:short)").split())


def repo_config(root):
    """Настройки репозитория по умолчанию из repos.json рядом со скриптом."""
    try:
        config = json.loads((HERE / "repos.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for key, value in config.items():
        if os.path.realpath(os.path.expanduser(key)) == root:
            return value
    return {}


def meta(repo):
    root = repo_root(repo)
    try:
        current = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except GitError:
        current = None
    return {"path": root, "name": os.path.basename(root), "current": current,
            "branches": branches(root), "config": repo_config(root)}


def new_path(path):
    """`a/{old => new}/b` и `old => new` из --numstat превращает в новый путь."""
    if " => " not in path:
        return path
    m = RENAME_BRACES.match(path)
    if m:
        pre, _old, new, post = m.groups()
        return (pre + new + post).replace("//", "/")
    return path.split(" => ", 1)[1]


def log(repo, rev, base=None):
    root = repo_root(repo)
    refs = all_refs(root)
    if rev == "--all":
        revs = ["--all"]
    elif rev in (None, "", "HEAD"):
        revs = ["HEAD"]
    elif rev in refs:
        revs = [rev]
    else:
        raise GitError(f"Нет такой ветки: {rev}")
    # base — чужая история (апстрим форка): её коммиты не считаем
    if base:
        if base not in refs:
            raise GitError(f"Нет такой ветки: {base}")
        revs.append("^" + base)

    out = git(root, "log", *revs, "--numstat", f"--format={LOG_FORMAT}")
    paths, path_index, commits = [], {}, []
    for record in out.split(REC)[1:]:
        head, _, body = record.partition("\n")
        parts = head.split(FLD)
        sha, parents, name, email, date = parts[:5]
        trailers = parts[-1]
        subject = FLD.join(parts[5:-1])
        files = []
        for line in body.splitlines():
            cols = line.split("\t", 2)
            if len(cols) != 3 or cols[0] == "-":  # пусто или бинарный файл
                continue
            path = new_path(cols[2])
            idx = path_index.get(path)
            if idx is None:
                idx = path_index[path] = len(paths)
                paths.append(path)
            files.append([idx, int(cols[0]), int(cols[1])])
        coauthors = [t.strip() for t in trailers.split("\x1d") if t.strip()]
        commits.append([sha, len(parents.split()) > 1, name, email, date,
                        subject, coauthors, files])
    return {"repo": meta(root), "rev": rev or "HEAD", "base": base or None,
            "paths": paths, "commits": commits}


def find_repos(roots, max_depth=4):
    found = []

    def walk(directory, depth):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        if any(e.name == ".git" for e in entries):
            git_dir = os.path.join(directory, ".git")
            mtime = max((os.path.getmtime(p) for p in
                         (git_dir, os.path.join(git_dir, "index"), os.path.join(git_dir, "HEAD"))
                         if os.path.exists(p)), default=0)
            found.append({"path": directory, "name": os.path.basename(directory), "mtime": mtime})
            return
        if depth >= max_depth:
            return
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP_DIRS:
                walk(e.path, depth + 1)

    for root in roots:
        walk(os.path.expanduser(root), 0)
    found.sort(key=lambda r: -r["mtime"])
    return found


class Handler(BaseHTTPRequestHandler):
    config = {}

    def log_message(self, fmt, *args):
        pass

    def send(self, status, body, content_type):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def json(self, payload, status=200):
        self.send(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                  "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/config":
                self.json(self.config)
            elif url.path == "/api/repos":
                self.json(find_repos(self.config["roots"]))
            elif url.path == "/api/meta":
                self.json(meta(q.get("repo")))
            elif url.path == "/api/log":
                self.json(log(q.get("repo"), q.get("rev"), q.get("base")))
            else:
                self.send(404, "not found", "text/plain; charset=utf-8")
        except GitError as e:
            self.json({"error": str(e)}, 400)


def main():
    parser = argparse.ArgumentParser(description="Дашборд статистики git")
    parser.add_argument("repo", nargs="?", default=os.getcwd())
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--roots", default="~/my,~/Work",
                        help="где искать репозитории для списка (через запятую)")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    try:
        default_repo = repo_root(args.repo)
    except GitError:
        default_repo = None
    Handler.config = {"defaultRepo": default_repo,
                      "roots": [r.strip() for r in args.roots.split(",") if r.strip()]}

    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit(f"Порты {args.port}–{args.port + 19} заняты")

    url = f"http://127.0.0.1:{port}/"
    print(f"git-stats: {url}  (Ctrl+C — выход)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
