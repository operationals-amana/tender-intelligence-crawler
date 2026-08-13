"""CLI entry point for scoring tenders.

Usage:
    python score_tenders.py                  # stage 1: heuristic score everything
    python score_tenders.py --llm            # stage 2: Claude deep-assesses the top 150
    python score_tenders.py --llm --llm-limit 50 --llm-rescore

Stage 1 is free and fast. Stage 2 costs money per tender, so it runs only over
the shortlist that stage 1 produced.
"""
import argparse

from api.scoring.runner import run_llm_assessment, score_tenders
from crawler.database import get_db, init_db


def main():
    parser = argparse.ArgumentParser(description="Score tenders against AMANA capabilities")
    parser.add_argument("--limit", type=int, default=None, help="Only score N tenders")
    parser.add_argument("--rescore-all", action="store_true", help="Recompute existing scores")
    parser.add_argument(
        "--include-expired", action="store_true", help="Also score closed tenders"
    )
    parser.add_argument(
        "--llm", action="store_true", help="Run the Claude deep-assessment stage afterwards"
    )
    parser.add_argument(
        "--llm-only", action="store_true", help="Skip the heuristic stage; only run Claude"
    )
    parser.add_argument("--llm-limit", type=int, default=300, help="How many tenders Claude assesses")
    parser.add_argument(
        "--llm-all-groups", action="store_true",
        help="Assess goods/works notices too, not just consulting services",
    )
    parser.add_argument(
        "--llm-min-score", type=float, default=None,
        help="Only assess tenders above this heuristic score",
    )
    parser.add_argument("--llm-rescore", action="store_true", help="Re-assess already-assessed tenders")
    parser.add_argument("--llm-concurrency", type=int, default=4, help="Parallel Claude requests")
    args = parser.parse_args()

    init_db()
    db = get_db()
    try:
        if not args.llm_only:
            score_tenders(
                db,
                limit=args.limit,
                rescore_all=args.rescore_all,
                include_expired=args.include_expired,
            )

        if args.llm or args.llm_only:
            print()
            run_llm_assessment(
                db,
                limit=args.llm_limit,
                min_heuristic_score=args.llm_min_score,
                rescore=args.llm_rescore,
                concurrency=args.llm_concurrency,
                consulting_only=not args.llm_all_groups,
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
