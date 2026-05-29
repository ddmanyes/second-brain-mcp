"""
benchmark.py — Scalability & accuracy evaluation for second-brain MCP server.

Usage:
    uv run python benchmark.py              # full benchmark
    uv run python benchmark.py --quick      # quick run (smaller sizes)
    uv run python benchmark.py --markdown   # output README-ready markdown

Metrics:
    Speed   : BM25 / semantic / hybrid latency (p50, p95) vs vault size
    Accuracy: Recall@1, Recall@3, Recall@5, MRR vs vault size
    Throughput: notes indexed/second, embeddings computed/second
"""

import argparse
import os
import random
import string
import sys
import tempfile
import time
from pathlib import Path
from statistics import median, quantiles

# Add parent dir to path so we can import vault_db directly
sys.path.insert(0, str(Path(__file__).parent))

import vault_db

# ---------------------------------------------------------------------------
# Ground truth queries — verified against the author's vault.
#
# NOTE for new users: these expected_path_substrings reference notes that exist
# in the author's personal vault. On a fresh install, Recall and MRR metrics
# will show 0% — this is expected. Search *latency* benchmarks are unaffected.
# To calibrate accuracy for your own vault, replace these entries with queries
# and note-path substrings from your own content.
# ---------------------------------------------------------------------------
GROUND_TRUTH: list[tuple[str, list[str]]] = [
    ("DuckDB full text search index",          ["vault-evolution", "vault_db"]),
    ("Ebbinghaus forgetting curve score",       ["vault-evolution", "ebbinghaus"]),
    ("PNG snapshot visual memory token",        ["phase-4-vision", "deepseek-ocr", "memocr"]),
    ("Gemini CLI image reading @filepath",      ["phase-4-vision", "文章圖表"]),
    ("bioinformatics single cell integration",  ["harmony", "fast-sensitive"]),
    ("LLM sleep consolidation compression",     ["simplemem", "experience-compression", "active-context"]),
    ("second brain MCP server architecture",    ["vault-evolution", "second-brain-system"]),
    ("archive prune old notes cleanup",         ["vault-evolution", "phase-8", "文章圖表"]),
    ("Pydantic FastMCP Image union type error", ["phase-4-vision", "文章圖表"]),
    ("memory compression spectrum missing diagonal", ["experience-compression", "second-brain-系統"]),
]

# ---------------------------------------------------------------------------
# Synthetic note generator
# ---------------------------------------------------------------------------
TOPICS = [
    ("machine learning", ["neural network", "gradient descent", "overfitting", "backprop", "loss function"]),
    ("database systems",  ["index", "query", "transaction", "schema", "normalisation"]),
    ("bioinformatics",    ["sequence alignment", "RNA-seq", "clustering", "cell type", "marker gene"]),
    ("compression",       ["token reduction", "quantisation", "entropy", "lossy", "lossless"]),
    ("agent memory",      ["episodic", "retrieval", "context window", "consolidation", "recall"]),
    ("visualisation",     ["matplotlib", "seaborn", "heatmap", "scatter plot", "axis label"]),
    ("software design",   ["abstraction", "interface", "dependency injection", "solid principles", "refactor"]),
    ("statistics",        ["p-value", "confidence interval", "distribution", "hypothesis test", "regression"]),
]


def _rand_word(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def generate_note(idx: int, vault: Path) -> Path:
    topic, keywords = random.choice(TOPICS)
    title = f"Synthetic Note {idx}: {topic.title()} Study"
    slug = f"synthetic-{idx:04d}-{topic.replace(' ', '-')}"
    body_sentences = []
    for _ in range(random.randint(8, 20)):
        kw = random.choice(keywords)
        body_sentences.append(
            f"This section covers {kw} in the context of {topic}. "
            f"Key insight: {_rand_word()} relates to {_rand_word()} via {kw}."
        )
    content = (
        f"---\ntitle: \"{title}\"\ndate: 2025-01-01\n"
        f"type: resource\nstatus: active\ntags: [{topic.replace(' ', '-')}]\n---\n\n"
        f"# {title}\n\n" + "\n".join(body_sentences)
    )
    dest = vault / "30-resources" / f"{slug}.md"
    dest.write_text(content, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Benchmark helpers
# ---------------------------------------------------------------------------

def _timed_calls(fn, n: int = 10) -> tuple[float, float]:
    """Return (p50_ms, p95_ms) over n calls."""
    times = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    p50 = median(times)
    p95 = quantiles(times, n=20)[18] if len(times) >= 5 else max(times)
    return round(p50, 1), round(p95, 1)


def recall_at_k(results: list[dict], expected: list[str], k: int) -> bool:
    paths = [r["path"] for r in results[:k]]
    return any(any(e in p for e in expected) for p in paths)


def reciprocal_rank(results: list[dict], expected: list[str]) -> float:
    for i, r in enumerate(results, 1):
        if any(e in r["path"] for e in expected):
            return 1.0 / i
    return 0.0


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------

def run_benchmark(sizes: list[int], n_reps: int = 10) -> dict:
    results = {}
    real_vault = Path(os.environ.get("SECOND_BRAIN_PATH", str(Path.home() / "second-brain")))

    for size in sizes:
        print(f"\n→ Vault size: {size} notes ...", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            (vault / "30-resources").mkdir(parents=True)
            (vault / "memory").mkdir()

            # Override DB and flags
            orig_db = vault_db.DB_PATH
            orig_auto = vault_db.EMBED_AUTO_START
            orig_schema = vault_db._schema_applied
            vault_db.DB_PATH = Path(tmp) / "bench.db"
            vault_db._schema_applied = False
            vault_db.EMBED_AUTO_START = False

            try:
                # Seed: copy real notes first (up to min(size, real count))
                real_notes = list(real_vault.rglob("*.md"))[:size]
                for src in real_notes:
                    rel = src.relative_to(real_vault)
                    dest = vault / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(src.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")

                # Fill remainder with synthetics
                synthetic_count = max(0, size - len(real_notes))
                for i in range(synthetic_count):
                    generate_note(i, vault)

                # Index (measure throughput)
                t0 = time.perf_counter()
                n_indexed = vault_db.sync_all(vault)
                index_time = time.perf_counter() - t0
                index_throughput = round(n_indexed / index_time) if index_time > 0 else 0

                # Attempt to sync embeddings (only if server is up)
                embed_throughput = None
                test_embed = vault_db.embed_text("test")
                if test_embed:
                    vault_db.EMBED_AUTO_START = True
                    t0 = time.perf_counter()
                    emb_result = vault_db.sync_embeddings()
                    embed_time = time.perf_counter() - t0
                    n_emb = emb_result["updated"]
                    if embed_time > 0 and n_emb > 0:
                        embed_throughput = round(n_emb / embed_time)

                # Speed benchmarks
                bm25_p50, bm25_p95 = _timed_calls(
                    lambda: vault_db.fts_search("memory compression", limit=5), n=n_reps
                )
                sem_p50, sem_p95 = _timed_calls(
                    lambda: vault_db.semantic_search("memory compression", limit=5), n=n_reps
                )
                hyb_p50, hyb_p95 = _timed_calls(
                    lambda: vault_db.hybrid_search("memory compression", limit=5), n=n_reps
                )

                # Accuracy: recall & MRR on ground truth queries
                r1_list, r3_list, r5_list, mrr_list = [], [], [], []
                for query, expected in GROUND_TRUTH:
                    hits = vault_db.hybrid_search(query, limit=10)
                    r1_list.append(recall_at_k(hits, expected, 1))
                    r3_list.append(recall_at_k(hits, expected, 3))
                    r5_list.append(recall_at_k(hits, expected, 5))
                    mrr_list.append(reciprocal_rank(hits, expected))

                r1 = round(sum(r1_list) / len(r1_list) * 100)
                r3 = round(sum(r3_list) / len(r3_list) * 100)
                r5 = round(sum(r5_list) / len(r5_list) * 100)
                mrr = round(sum(mrr_list) / len(mrr_list), 3)

                results[size] = {
                    "n_real": len(real_notes),
                    "n_synthetic": synthetic_count,
                    "bm25_p50": bm25_p50, "bm25_p95": bm25_p95,
                    "sem_p50": sem_p50,  "sem_p95": sem_p95,
                    "hyb_p50": hyb_p50,  "hyb_p95": hyb_p95,
                    "index_throughput": index_throughput,
                    "embed_throughput": embed_throughput,
                    "recall_1": r1, "recall_3": r3, "recall_5": r5, "mrr": mrr,
                }
                print(
                    f"   BM25 p50={bm25_p50}ms  Semantic p50={sem_p50}ms  Hybrid p50={hyb_p50}ms  "
                    f"R@1={r1}%  R@5={r5}%  MRR={mrr}"
                )
            finally:
                vault_db.DB_PATH = orig_db
                vault_db._schema_applied = orig_schema
                vault_db.EMBED_AUTO_START = orig_auto

    return results


def print_markdown(results: dict) -> None:
    print("\n## Benchmark Results\n")
    print("### Search Latency (p50 / p95 ms)\n")
    print("| Notes | BM25 p50 | BM25 p95 | Semantic p50 | Semantic p95 | Hybrid p50 | Hybrid p95 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for size, r in sorted(results.items()):
        sem_note = "¹" if r["embed_throughput"] is None else ""
        print(f"| {size} | {r['bm25_p50']} | {r['bm25_p95']} | "
              f"{r['sem_p50']}{sem_note} | {r['sem_p95']}{sem_note} | "
              f"{r['hyb_p50']} | {r['hyb_p95']} |")
    print("\n> ¹ Semantic search unavailable (llama-server not running) — fell back to BM25\n")

    print("### Search Accuracy (Hybrid, 10 labeled queries)\n")
    print("| Notes | Recall@1 | Recall@3 | Recall@5 | MRR |")
    print("| --- | --- | --- | --- | --- |")
    for size, r in sorted(results.items()):
        print(f"| {size} | {r['recall_1']}% | {r['recall_3']}% | {r['recall_5']}% | {r['mrr']} |")

    print("\n### Indexing Throughput\n")
    print("| Notes | Index (notes/s) | Embed (notes/s) |")
    print("| --- | --- | --- |")
    for size, r in sorted(results.items()):
        emb = str(r["embed_throughput"]) if r["embed_throughput"] else "N/A (no llama-server)"
        print(f"| {size} | {r['index_throughput']} | {emb} |")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick",    action="store_true", help="Quick run: 10,50,100")
    parser.add_argument("--markdown", action="store_true", help="Output README markdown")
    parser.add_argument("--reps",     type=int, default=10, help="Repetitions per timing")
    args = parser.parse_args()

    sizes = [10, 50, 100] if args.quick else [10, 50, 100, 500, 1000]
    print(f"Running benchmark at sizes: {sizes}  (reps={args.reps})")

    results = run_benchmark(sizes, n_reps=args.reps)

    if args.markdown:
        print_markdown(results)
    else:
        print("\n=== Summary ===")
        for size, r in sorted(results.items()):
            print(f"n={size:4d}  BM25={r['bm25_p50']}ms  "
                  f"Hybrid={r['hyb_p50']}ms  R@5={r['recall_5']}%  MRR={r['mrr']}")
