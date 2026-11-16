#!/usr/bin/env python3
"""推送前的依赖完整性检查：候选文件之间「有没有漏推」。

背景：48c3fcf0 那次只提交了 app.py，漏了同一轮改过的 travel_agent/agent.py。
线上拿到「新 app.py + 旧 agent.py」，后台线程抛 TypeError，收尾时
`isinstance(exc, agent.AgentCancelled)` 又找不到符号，直接 AttributeError 整页崩红。
「只提交本次改动的文件」这个策略本身没问题，缺的是「改动文件之间的依赖有没有
一起带上」这道检查，本脚本补的就是它。

判断口径（以「将要推送的文件集合」为单位，不看工作区全量）：
1. 从候选文件出发解析仓库内 Python import，得到本地依赖图；
2. 依赖到的文件若【不在候选集合】里，就必须与远端同路径内容完全一致
   （git blob sha 相等），否则就是「本地改了但没推」——正是上次那种漏推；
3. declare_component(path=...) 挂的组件目录按目录递归计入同样判断，
   index.html 这类静态资源也一起查，不只扫 .py；
4. 包路径上的 __init__.py 自动补进依赖，避免只推子模块导致 import 失败。

用法：
    python tools/preflight_push.py --files app.py
    python tools/preflight_push.py --files app.py,components/map_intro.py
    python tools/preflight_push.py --files app.py --remote-ref 48c3fcf0

退出码：0 通过 / 1 依赖不完整 / 2 环境或参数问题（拿不到远端树等）。
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = os.environ.get("PREFLIGHT_REPO", "Dwy0418/travel-agent")
SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "shots", "logs"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}


# ---------------------------------------------------------------------------
# 远端 tree / blob sha
# ---------------------------------------------------------------------------


def _gh_json(endpoint: str) -> dict:
    try:
        out = subprocess.run(
            ["gh", "api", endpoint],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except FileNotFoundError:
        print("[preflight] 找不到 gh 命令，无法读取远端 tree", file=sys.stderr)
        raise SystemExit(2)
    except subprocess.CalledProcessError as exc:
        print(f"[preflight] gh api {endpoint} 失败：{exc.stderr.strip()[:300]}", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(out)


def remote_snapshot(repo: str, ref: str) -> tuple[dict[str, str], str, str]:
    """返回 (path -> blob sha, tree sha, 解析后的 commit sha)。"""
    commit = _gh_json(f"repos/{repo}/commits/{ref}")
    tree_sha = commit["commit"]["tree"]["sha"]
    tree = _gh_json(f"repos/{repo}/git/trees/{tree_sha}?recursive=1")
    blobs = {item["path"]: item["sha"] for item in tree["tree"] if item.get("type") == "blob"}
    return blobs, tree_sha, commit["sha"]


def _normalized(data: bytes) -> bytes:
    """行尾归一化：CRLF / LF 只是编辑器差异，不该算「内容不一致」。

    Windows 上把这几个文件存成 CRLF 后，blob sha 必然和远端（LF）不同，
    只按 sha 判断会把一堆无辜文件报成漏推，检查就没法用了。
    """
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def remote_blob_bytes(repo: str, sha: str) -> bytes:
    payload = _gh_json(f"repos/{repo}/git/blobs/{sha}")
    return base64.b64decode(payload["content"])


# ---------------------------------------------------------------------------
# import 解析
# ---------------------------------------------------------------------------


def _module_paths(parts: list[str]) -> list[str]:
    """模块名 -> 仓库内的候选文件路径（模块文件 + 包入口）。"""
    if not parts:
        return []
    base = "/".join(parts)
    return [f"{base}.py", f"{base}/__init__.py"]


def _init_chain(rel_path: str) -> list[str]:
    """一个文件所在包链上的 __init__.py（Python 导入子模块时必须存在）。"""
    parent = Path(rel_path).parent
    out: list[str] = []
    while parent.parts and str(parent) not in (".", ""):
        out.append((parent / "__init__.py").as_posix())
        parent = parent.parent
    return out


def _resolve_from(rel_path: str, module: str | None, level: int) -> list[str]:
    if not level:
        return (module or "").split(".") if module else []
    pkg = Path(rel_path).parent
    for _ in range(level - 1):
        pkg = pkg.parent
    parts = [] if str(pkg) in (".", "") else list(pkg.parts)
    if module:
        parts += module.split(".")
    return parts


# --- declare_component(path=...) 的路径表达式求值 -------------------------
# 只支持本仓库实际在用的两种写法，求不出来就跳过并提示，不猜：
#   path=str(Path(__file__).parent / "map_intro_component")
#   path=str(_COMPONENT_DIR)  （_COMPONENT_DIR 在模块级赋值）
_DIR = "\0dir"  # 形如 Path(__file__).parent


def _eval_path(node: ast.AST, env: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = node.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "Path"
            and inner.args
            and isinstance(inner.args[0], ast.Name)
            and inner.args[0].id == "__file__"
        ):
            return _DIR
        return None
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "str" and node.args:
            return _eval_path(node.args[0], env)
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _eval_path(node.left, env)
        right = _eval_path(node.right, env)
        if left == _DIR and isinstance(right, str):
            return right  # 相对本文件所在目录的子目录
        return None
    return None


def component_dirs(rel_path: str, tree: ast.Module) -> tuple[list[str], list[str]]:
    """找 declare_component 挂的组件目录，返回 (目录相对路径, 无法求值的表达式文本)。"""
    env: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = _eval_path(node.value, env)
            if isinstance(value, str):
                env[node.targets[0].id] = value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            value = _eval_path(node.value, env)
            if isinstance(value, str):
                env[node.target.id] = value

    dirs: list[str] = []
    unresolved: list[str] = []
    owner_dir = Path(rel_path).parent
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "declare_component":
            continue
        arg = next((kw.value for kw in node.keywords if kw.arg == "path"), None)
        if arg is None and len(node.args) >= 2:
            arg = node.args[1]
        if arg is None:
            continue
        value = _eval_path(arg, env)
        if not value or value == _DIR:
            unresolved.append(ast.unparse(arg))
            continue
        rel = (owner_dir / value).as_posix() if str(owner_dir) not in (".", "") else value
        dirs.append(rel.lstrip("./"))
    return dirs, unresolved


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def collect_dependencies(candidate_files: list[str]) -> tuple[dict[str, str], list[str]]:
    """从候选 .py 出发做 BFS，沿 import 与组件目录递归，返回
    ({被引用的仓库内文件: 引用者}, [无法静态求值的组件路径表达式])。"""
    deps: dict[str, str] = {}
    unresolved_components: list[str] = []
    queue = [p for p in candidate_files if p.endswith(".py")]
    seen: set[str] = set()

    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"[preflight] 跳过无法解析的文件 {rel}: {exc}", file=sys.stderr)
            continue

        # 1) import 关系
        for node in ast.walk(tree):
            targets: list[list[str]] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    targets.append(alias.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                parts = _resolve_from(rel, node.module, node.level)
                if parts:
                    targets.append(parts)
                if node.module and not node.level:
                    for alias in node.names:  # from X import 子模块
                        if alias.name != "*":
                            targets.append(node.module.split(".") + [alias.name])
            for parts in targets:
                hit = next((p for p in _module_paths(parts) if (ROOT / p).exists()), None)
                if hit is None:
                    continue  # 标准库/第三方，交给 requirements 管
                deps.setdefault(hit, rel)
                for init in _init_chain(hit):
                    if (ROOT / init).exists():
                        deps.setdefault(init, rel)
                # 本地依赖也要继续往下解析：组件目录是挂在 map_intro.py 上的，
                # 只扫候选文件会漏掉 index.html 这类静态资源。
                queue.append(hit)

        # 2) 组件目录（含 index.html 等静态资源）
        dirs, unresolved = component_dirs(rel, tree)
        unresolved_components += [f"{rel}: {u}" for u in unresolved]
        for cdir in dirs:
            base = ROOT / cdir
            if not base.exists():
                continue
            for item in sorted(base.rglob("*")):
                if not item.is_file():
                    continue
                if SKIP_DIRS & set(item.parts) or item.name in SKIP_NAMES:
                    continue
                deps.setdefault(item.relative_to(ROOT).as_posix(), rel)

    return deps, unresolved_components


def main() -> int:
    parser = argparse.ArgumentParser(description="推送前检查：候选文件之间有没有漏推依赖")
    parser.add_argument("--files", required=True, nargs="+", help="将要推送的文件（逗号或空格分隔）")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/repo，默认 {DEFAULT_REPO}")
    parser.add_argument("--remote-ref", default="main", help="对比的远端 ref/sha，默认 main")
    args = parser.parse_args()

    raw = [item for group in args.files for item in group.replace(",", " ").split()]
    candidates: list[str] = []
    for item in raw:
        rel = Path(item.replace("\\", "/")).as_posix().lstrip("./")
        full = ROOT / rel
        if not full.exists():
            print(f"[preflight] 候选文件不存在：{rel}", file=sys.stderr)
            return 2
        candidates.append(rel)

    blobs, tree_sha, commit_sha = remote_snapshot(args.repo, args.remote_ref)
    print(f"[preflight] repo={args.repo} ref={args.remote_ref} commit={commit_sha[:7]} tree={tree_sha[:7]}")
    print(f"[preflight] 候选 {len(candidates)} 个文件：{', '.join(candidates)}")

    deps, unresolved = collect_dependencies(candidates)
    for note in unresolved:
        print(f"[preflight] 提示：组件路径表达式无法静态求值，请人工确认 → {note}")

    missing: list[tuple[str, str, str]] = []
    same = 0
    eol_only: list[str] = []
    for dep, referrer in sorted(deps.items()):
        if dep in candidates:
            continue
        remote_sha = blobs.get(dep)
        if remote_sha is None:
            missing.append((dep, referrer, "远端不存在"))
            continue
        local_bytes = (ROOT / dep).read_bytes()
        if hashlib.sha1(b"blob %d\0" % len(local_bytes) + local_bytes).hexdigest() != remote_sha:
            if _normalized(local_bytes) != _normalized(remote_blob_bytes(args.repo, remote_sha)):
                missing.append(
                    (dep, referrer, f"远端内容不一致（本地 {len(local_bytes)} 字节 / 远端 blob {remote_sha[:7]}）")
                )
                continue
            eol_only.append(dep)
        same += 1

    print(f"[preflight] 追踪到仓库内依赖 {len(deps)} 个：候选内 {len([d for d in deps if d in candidates])} 个，"
          f"远端已一致 {same} 个，问题 {len(missing)} 个")
    if eol_only:
        print(f"[preflight] {len(eol_only)} 个文件仅行尾差异（CRLF/LF），按一致处理：{', '.join(eol_only)}")

    if missing:
        print(f"\nFAIL 依赖不完整：{len(missing)} 个被引用的文件既不在候选里、也和远端不一致")
        for dep, referrer, reason in missing:
            print(f"  ✗ {dep}")
            print(f"      引用者: {referrer}")
            print(f"      原因:   {reason}")
        print("\n修复：把上面这些文件加进 --files，再执行推送。")
        return 1

    print("\nPASS 依赖完整：候选集合已覆盖所有被引用的仓库内文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
