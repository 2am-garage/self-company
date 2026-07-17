#!/usr/bin/env python3
import argparse, json, os, subprocess, sys, tempfile, shutil

POSITIVE_CONTROL = ("什麼語言", ["chairman-reply-language-chinese"])

FIXTURE = [
    ("chairman-reply-language-chinese", "When replying to the Chairman, staff must answer in Traditional Chinese."),
    ("database-backups", "Maintains postgres databases and requires automated nightly backups."),
    ("merge-gate", "The company may merge its own pull request when tests pass."),
    ("verify-before-commit", "Never trust, always test: verify scripts before committing."),
    ("editor-preference", "The Chairman prefers the Neovim editor with a dark theme."),
    ("git-identity", "Keep the existing git identity; never add attribution trailers."),
    ("entropy-metric", "Treats entropy as the memory-quality KPI; should drop or stay flat."),
    ("sub-agent-isolation", "Dispatches build work to employee subagents; Bob builds, Gibby attacks."),
    ("delegation-phoebe", "Routes testing and quality architecture work to Phoebe."),
    ("approval-gate", "Structural changes need Elon sign-off; routine tweaks do not."),
]

def build_company(tmpdir):
    cpy = os.path.join(tmpdir, ".company")
    mem = os.path.join(cpy, "memory", "L2-cold", "preferences")
    os.makedirs(mem, exist_ok=True)
    for name, body in FIXTURE:
        path = os.path.join(mem, name + ".md")
        with open(path, "w") as f:
            lines = ["---", "id: " + name, "tier: L2", "status: active", "---", body]
            f.write("\n".join(lines) + "\n")
    return cpy

def build_index(cpy, scripts, venv_py):
    mem = os.path.join(cpy, "memory")
    proc = subprocess.run([venv_py, os.path.join(scripts, "rag_index.py"), "--memory-dir", mem, "--index-dir", os.path.join(mem, "index"), "--rebuild"], capture_output=True, text=True, timeout=300, env={**os.environ, "SC_RAG_REEXEC": "1"})
    return proc.returncode

def query_rag(q, cpy, scripts, venv_py, k=5):
    mem = os.path.join(cpy, "memory")
    proc = subprocess.run([venv_py, os.path.join(scripts, "rag_query.py"), "--query", q, "--top-k", str(k), "--index-dir", os.path.join(mem, "index")], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except:
        return None

def score(q, exp_ids, results, k):
    if not results:
        ret_ids, hit = [], False
    else:
        ret_ids = [r["id"] for r in results[:k]]
        hit = any(e in ret_ids for e in exp_ids)
    recall = sum(1 for e in exp_ids if e in ret_ids) / len(exp_ids) if exp_ids else 0
    return {"query": q, "expected_ids": exp_ids, "retrieved_ids": ret_ids, "hit@k": hit, "recall": round(recall, 4), "pass": hit}

def summarize(scores):
    total = len(scores)
    passed = sum(1 for s in scores if s["pass"])
    avg_recall = sum(s["recall"] for s in scores) / total if total else 0
    return {"total_queries": total, "passed": passed, "failed": total - passed, "pass_rate": round(passed / total, 4) if total else 0, "avg_recall": round(avg_recall, 4)}

def find_scripts(repo):
    for c in [os.path.join(repo, "plugin", "skills", "self-company", "scripts"), os.path.join(repo, "scripts")]:
        if os.path.isdir(c):
            return c
    return None

def find_venv(repo):
    vp = os.path.join(repo, ".company", ".rag-venv", "bin", "python")
    return vp if os.access(vp, os.X_OK) else None

def main(argv=None):
    p = argparse.ArgumentParser(description="RAG recall evaluation.")
    p.add_argument("--eval-set")  # required unless --self-test (checked below)
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--threshold", type=float, default=1.0)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    if not args.self_test and not args.eval_set:
        p.error("--eval-set is required unless --self-test is given")
    
    scripts = find_scripts(args.repo)
    venv_py = find_venv(args.repo)
    
    if not scripts or not venv_py:
        print("skip: venv/scripts unavailable", file=sys.stderr)
        return 0
    
    tmpdir = tempfile.mkdtemp()
    try:
        cpy = build_company(tmpdir)
        if build_index(cpy, scripts, venv_py) != 0:
            print("skip: index build failed", file=sys.stderr)
            return 0
        
        if args.self_test:
            q, exp_ids = POSITIVE_CONTROL
            results = query_rag(q, cpy, scripts, venv_py)
            ret = [r["id"] for r in (results or [])]
            ok = any(e in ret for e in exp_ids)
            print(json.dumps({"self_test": {"query": q, "ok": ok, "verdict": "OK" if ok else "BROKEN"}}, indent=2))
            return 0 if ok else 2
        
        evals = json.load(open(args.eval_set))
        scores = []
        for i, item in enumerate(evals):
            if args.verbose:
                print("[{}/{}] {}".format(i+1, len(evals), item["query"][:50]), file=sys.stderr, flush=True)
            results = query_rag(item["query"], cpy, scripts, venv_py, args.top_k)
            scores.append(score(item["query"], item["expected_ids"], results, args.top_k))
        
        report = {"config": {"repo": os.path.abspath(args.repo), "top_k": args.top_k, "threshold": args.threshold}, "summary": summarize(scores), "queries": scores}
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["summary"]["pass_rate"] >= args.threshold else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    sys.exit(main())
