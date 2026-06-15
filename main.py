"""Facebook Rental Radar Bot — entry point."""
import argparse
import asyncio
import subprocess
import sys
import os
import random

import config
import database.db as db
from analysis.extractor import (
    ExtractionError, validate_model,
    check_relevance, extract_listings_with_comments,
)
from output.excel import export_excel


class DatabaseError(Exception):
    pass


def parse_args():
    p = argparse.ArgumentParser(description="Facebook Rental Radar Bot")
    p.add_argument("--login",       action="store_true", help="Open browser for FB login")
    p.add_argument("--export-only", action="store_true", help="Export Excel from DB, no scrape")
    p.add_argument("--max-posts",   type=int, default=None, help="Limit posts per run")
    p.add_argument("--cleanup",     action="store_true", help="Delete old posts from DB")
    return p.parse_args()


def process_listing(post_id: int, raw_listing: dict) -> None:
    rent = raw_listing.get("rent")
    try:
        raw_listing["move_in_cost"] = (float(rent) * 3) if rent else None
    except (ValueError, TypeError):
        raw_listing["move_in_cost"] = None
    try:
        db.insert_listing(post_id, raw_listing)
    except Exception as e:
        raise DatabaseError(f"DB insert failed: {e}") from e



async def run_scrape(args):
    from scraper.browser import (
        SessionExpiredError,
        USER_DATA_DIR,
        describe_session_status,
        is_session_valid,
        launch_context,
    )
    from scraper.feed import scrape_group

    if not config.FB_GROUP_URLS:
        print("ERROR: FB_GROUP_URLS not set in .env")
        sys.exit(1)

    max_posts = args.max_posts or config.MAX_POSTS_PER_RUN

    print("[phase] launch_browser")
    pw, context = await launch_context(headless=config.HEADLESS)
    print(f"Using Facebook profile: {USER_DATA_DIR}")

    run_id = db.create_run()
    stats = {"groups_scanned": 0, "posts_found": 0, "listings_extracted": 0}

    try:
        print("[phase] session_check")
        session_ok, session_info = await is_session_valid(context)
        if not session_ok:
            print("Session expired - Please run --login again")
            print(f"  {describe_session_status(session_info)}")
            raise SessionExpiredError("Facebook session expired before starting scrape")

        total_groups = len(config.FB_GROUP_URLS)
        for group_index, group_url in enumerate(config.FB_GROUP_URLS, start=1):
            print(f"[group] begin index={group_index} total={total_groups} url={group_url}")
            posts = await scrape_group(
                context, group_url,
                max_scroll_rounds=config.MAX_SCROLL_ROUNDS,
                max_posts=max_posts,
            )
            stats["groups_scanned"] += 1
            print(f"[group] collected index={group_index} posts={len(posts)}")

            for post_data in posts:
                stats["posts_found"] += 1
                post_ref = post_data.get("post_id") or post_data.get("post_url") or f"post#{stats['posts_found']}"

                post_id, is_new, duplicate_reason = db.upsert_post(run_id, post_data)
                if post_id is None:
                    print(f"[post-db] status=skipped post={post_ref} reason=no_db_id")
                    continue

                if post_data.get("status") == "prefiltered_skip":
                    print(f"[post-db] status=prefiltered_skip post={post_ref}")
                    db.update_post_status(post_id, "prefiltered_skip")
                    continue

                if not is_new:
                    duplicate_status = "duplicate_post_id" if duplicate_reason == "post_id" else "duplicate_cross_group"
                    print(f"[post-db] status={duplicate_status} post={post_ref} db_id={post_id}")
                    continue

                print(
                    f"[post-db] status=new post={post_ref} db_id={post_id} "
                    f"images={len(post_data.get('image_urls', []))} "
                    f"comments={len(post_data.get('comments', []))}"
                )
                for img_url in post_data.get("image_urls", []):
                    db.insert_post_image(post_id, img_url)

                comments = post_data.get("comments", [])
                if comments:
                    db.insert_post_comments(post_id, comments)

                post_redacted = post_data.get("raw_text_redacted") or post_data.get("raw_text", "")

                try:
                    print(f"[relevance] check post={post_ref} comments={len(comments)}")
                    relevant = await asyncio.to_thread(check_relevance, post_redacted, comments)
                    if not relevant:
                        print(f"[relevance] skip post={post_ref} reason=not_relevant")
                        db.update_post_status(post_id, "prefiltered_skip")
                        continue

                    print(f"[extract] start post={post_ref} mode=post_with_comments comments={len(comments)}")
                    listings = await asyncio.to_thread(
                        extract_listings_with_comments, post_redacted, comments
                    )

                    print(f"[extract] success post={post_ref} listings={len(listings)}")
                    db.update_post_status(post_id, "extracted")
                except ExtractionError as e:
                    print(f"  [!] Extraction failed: {e}")
                    print(f"[extract] failed post={post_ref} error={e}")
                    db.update_post_status(post_id, "extract_failed")
                    continue

                for raw_listing in listings:
                    try:
                        process_listing(post_id, raw_listing)
                    except DatabaseError as e:
                        print(f"[FATAL] {e}")
                        print("[FATAL] Cannot write to database — stopping scrape.")
                        db.fail_run(run_id)
                        return stats
                    stats["listings_extracted"] += 1
                    print(
                        f"[listing] condo={raw_listing.get('condo_name') or '-'} "
                        f"rent={raw_listing.get('rent') or '-'} "
                        f"size={raw_listing.get('size_sqm') or '-'}"
                    )

                db.update_post_status(post_id, "extracted")
                print(f"[post-db] status=extracted post={post_ref} listings={len(listings)}")

            if group_index < total_groups:
                sleep_time = random.uniform(5.0, 12.0)
                print(
                    "[group] pause "
                    f"index={group_index} next_index={group_index + 1} "
                    f"sleep={sleep_time:.2f}s reason=human_like_group_switch"
                )
                await asyncio.sleep(sleep_time)
        db.finish_run(run_id, stats)

    except SessionExpiredError:
        db.fail_run(run_id)
        raise
    except Exception:
        db.fail_run(run_id)
        raise
    finally:
        await context.close()
        await pw.stop()

    return stats


def main():
    args = parse_args()
    submitted_debug = os.getenv("WEB_DEBUG_FORM_VALUES")
    db.init_db()

    from scraper.browser import run_login

    # --login: open browser for FB login regardless of credential state
    if args.login:
        asyncio.run(run_login())
        return

    # --cleanup / --export-only don't need FB credentials
    if args.cleanup:
        db.cleanup_old_posts(config.DATA_RETENTION_DAYS)
        print(f"Cleaned up posts older than {config.DATA_RETENTION_DAYS} days.")
        return

    if args.export_only:
        rows = db.get_listings()
        path = export_excel(rows)
        print(f"Exported: {path}")
        return

    # Validate DeepSeek model on startup
    print(f"[phase] validating_model model={config.DEEPSEEK_MODEL}")
    print(f"Validating DeepSeek model '{config.DEEPSEEK_MODEL}'...")
    try:
        validate_model()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    async def _run():
        if submitted_debug:
            print(f"[web-form] {submitted_debug}")
        print(
            f"[phase] scrape_start groups={len(config.FB_GROUP_URLS)} "
            f"max_posts={args.max_posts or config.MAX_POSTS_PER_RUN}"
        )
        print(f"Starting scrape — max {args.max_posts or config.MAX_POSTS_PER_RUN} posts, "
              f"{len(config.FB_GROUP_URLS)} group(s)")
        stats = await run_scrape(args)
        print(
            f"[phase] scrape_done groups={stats['groups_scanned']} "
            f"posts={stats['posts_found']} listings={stats['listings_extracted']}"
        )
        print(f"\nDone — posts: {stats['posts_found']}, listings: {stats['listings_extracted']}")
        print("[phase] export_start")
        rows = db.get_listings()
        if rows:
            path = export_excel(rows)
            print(f"Excel saved: {path}")
            print(f"[phase] export_done path={path}")
            subprocess.run(["open", path], check=False)
        else:
            print("No listings to export.")
            print("[phase] export_done path=none")

    from scraper.browser import SessionExpiredError

    if config.AUTO_LOOP:
        interval_sec = config.LOOP_INTERVAL_MINUTES * 60
        print(f"[loop] AUTO_LOOP=on interval={config.LOOP_INTERVAL_MINUTES}m — press Ctrl+C to stop")
        round_num = 0
        while True:
            round_num += 1
            print(f"\n[loop] round={round_num} start")
            try:
                asyncio.run(_run())
            except SessionExpiredError:
                print("[loop] session expired — stopping loop")
                sys.exit(2)
            except Exception as e:
                print(f"[loop] round={round_num} error={e} — will retry next interval")
            print(f"[loop] round={round_num} done — sleeping {config.LOOP_INTERVAL_MINUTES}m")
            try:
                import time
                time.sleep(interval_sec)
            except KeyboardInterrupt:
                print("\n[loop] stopped by user")
                break
    else:
        try:
            asyncio.run(_run())
        except SessionExpiredError:
            sys.exit(2)


if __name__ == "__main__":
    main()
