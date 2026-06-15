"""Facebook Rental Radar Bot — entry point."""
import argparse
import asyncio
import json
import subprocess
import sys
import os
import random

import config
import database.db as db
from rules.preferences import load_preferences
from rules.scoring import score_listing, TIER_ORDER, apply_vision
from analysis.extractor import (
    extract_listings, compute_move_in_cost, compute_fingerprint,
    ExtractionError, extract_listings_from_comment, validate_model,
)
from output.excel import export_excel
from alerts.notify import alert_if_new


def parse_args():
    p = argparse.ArgumentParser(description="Facebook Rental Radar Bot")
    p.add_argument("--login",         action="store_true", help="Open browser for FB login")
    p.add_argument("--enable-vision", action="store_true", help="Score images with GPT-4o-mini")
    p.add_argument("--export-only",   action="store_true", help="Export Excel from DB, no scrape")
    p.add_argument("--max-posts",     type=int, default=None, help="Limit posts per run")
    p.add_argument("--tier",          type=str, default=None,
                   help="Print listings of tier: must-call, shortlist, need-info, maybe")
    p.add_argument("--cleanup",       action="store_true", help="Delete old posts from DB")
    return p.parse_args()


def print_tier(tier_slug: str):
    rows = db.get_listings_with_scores(tier_filter=tier_slug)
    if not rows:
        print(f"No listings in tier: {tier_slug}")
        return
    for row in rows:
        score = row["final_total"] or 0
        rent = row["monthly_rent"]
        condo = row["condo_name"] or row["location_text"] or "?"
        station = row["station_name"] or ""
        url = row["post_url"] or ""
        rent_str = f"฿{int(rent):,}" if rent else "ราคาไม่ระบุ"
        print(f"  [{score:.0f}] {condo} {station} — {rent_str} — {url}")


def process_listing(post_id: int, raw_listing: dict, prefs, enable_vision: bool,
                    base64_images: list[str]) -> str:
    """Extract → score → DB insert one listing. Returns tier slug."""
    # Compute derived fields
    raw_listing["move_in_cost"] = compute_move_in_cost(raw_listing)
    fingerprint = compute_fingerprint(raw_listing)

    # Duplicate check
    dup = db.get_listing_by_fingerprint(fingerprint)
    raw_listing["duplicate_flag"] = 1 if dup else 0
    if raw_listing["duplicate_flag"]:
        flags = json.loads(raw_listing.get("risk_flags") or "[]")
        if "possible duplicate" not in flags:
            flags.append("possible duplicate")
        raw_listing["risk_flags"] = json.dumps(flags, ensure_ascii=False)

    raw_listing["listing_fingerprint"] = fingerprint
    raw_listing["risk_flags"] = json.dumps(raw_listing.get("risk_flags") or [], ensure_ascii=False)
    raw_listing["missing_fields"] = json.dumps(raw_listing.get("missing_fields") or [], ensure_ascii=False)
    raw_listing["questions_to_ask"] = json.dumps(raw_listing.get("questions_to_ask") or [], ensure_ascii=False)

    listing_id = db.insert_listing(post_id, raw_listing)

    # Score
    score_data = score_listing(raw_listing, prefs)

    # Vision pass (only for must_call / shortlist, and only when enabled)
    if enable_vision and score_data["tier"] in ("must_call", "shortlist") and base64_images:
        from analysis.vision import score_images
        v_score = score_images(base64_images)
        if v_score is not None:
            from rules.scoring import apply_vision, base_score
            base_result = base_score(raw_listing, prefs)
            upgraded = apply_vision(base_result, v_score)
            score_data.update({
                "vision_score": v_score,
                "condition_score": upgraded["condition_score"],
                "final_total": upgraded["final_total"],
                "tier": upgraded["tier"],
            })

    score_data["final_total"] = score_data.get("final_total") or score_data.get("base_total")
    db.insert_score(listing_id, score_data)
    return score_data["tier"]


async def extract_listings_from_seeking_comments(
    post_text_redacted: str,
    comments: list[str],
    post_ref: str,
) -> list[dict]:
    semaphore = asyncio.Semaphore(config.SEEKING_COMMENT_EXTRACT_CONCURRENCY)

    async def _extract_one(index: int, comment_text: str) -> list[dict]:
        async with semaphore:
            try:
                listings = await asyncio.to_thread(
                    extract_listings_from_comment,
                    post_text_redacted,
                    comment_text,
                )
                print(
                    f"[extract-comment] post={post_ref} index={index} "
                    f"listings={len(listings)}"
                )
                return listings
            except ExtractionError as e:
                print(f"[extract-comment] failed post={post_ref} index={index} error={e}")
                return []
            except Exception as e:
                print(f"[extract-comment] crashed post={post_ref} index={index} error={e}")
                return []

    tasks = [
        asyncio.create_task(_extract_one(index, comment))
        for index, comment in enumerate(comments, start=1)
    ]
    results = await asyncio.gather(*tasks)
    listings: list[dict] = []
    for chunk in results:
        listings.extend(chunk)
    return listings


async def run_scrape(args, prefs):
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
    enable_vision = args.enable_vision or config.ENABLE_VISION

    print("[phase] launch_browser")
    pw, context = await launch_context(headless=config.HEADLESS)
    print(f"Using Facebook profile: {USER_DATA_DIR}")

    run_id = db.create_run()
    stats = {"groups_scanned": 0, "posts_found": 0, "listings_extracted": 0, "must_call_count": 0}

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
                enable_vision=enable_vision,
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
                post_intent = post_data.get("post_intent", "for_rent")

                try:
                    if post_intent == "seeking":
                        print(
                            f"[extract] start post={post_ref} mode=seeking_comments "
                            f"accepted_comments={len(comments)}"
                        )
                        listings = await extract_listings_from_seeking_comments(
                            post_redacted,
                            comments,
                            str(post_ref),
                        )
                    else:
                        redacted = post_redacted

                        print(f"[extract] start post={post_ref} mode=post_blob")
                        listings = extract_listings(redacted)

                    print(f"[extract] success post={post_ref} listings={len(listings)}")
                    db.update_post_status(post_id, "extracted")
                except ExtractionError as e:
                    print(f"  [!] Extraction failed: {e}")
                    print(f"[extract] failed post={post_ref} error={e}")
                    db.update_post_status(post_id, "extract_failed")
                    continue

                b64_images = post_data.get("base64_images", [])
                post_risk_flags = []
                if post_data.get("broken_images_filtered"):
                    post_risk_flags.append("broken_images_filtered")

                for raw_listing in listings:
                    flags = raw_listing.get("risk_flags") or []
                    if isinstance(flags, str):
                        try:
                            flags = json.loads(flags)
                        except json.JSONDecodeError:
                            flags = [flags] if flags else []
                    for flag in post_risk_flags:
                        if flag not in flags:
                            flags.append(flag)
                    raw_listing["risk_flags"] = flags

                    tier = process_listing(post_id, raw_listing, prefs, enable_vision, b64_images)
                    stats["listings_extracted"] += 1
                    print(
                        f"[listing] tier={tier} condo={raw_listing.get('condo_name') or '-'} "
                        f"rent={raw_listing.get('monthly_rent') or '-'} "
                        f"size={raw_listing.get('size_sqm') or '-'}"
                    )
                    if tier == "must_call":
                        stats["must_call_count"] += 1
                        print(f"  ★ MUST CALL: {raw_listing.get('condo_name')} "
                              f"฿{raw_listing.get('monthly_rent', '?')}/mo")
                    elif tier == "shortlist":
                        print(f"  ✓ Shortlist: {raw_listing.get('condo_name')} "
                              f"฿{raw_listing.get('monthly_rent', '?')}/mo")

                db.update_post_status(post_id, "scored")
                print(f"[post-db] status=scored post={post_ref} listings={len(listings)}")

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


def run_alerts(prefs):
    min_idx = TIER_ORDER.index(prefs.alert_min_tier.lower())
    rows = db.get_unalerted_above_tier(min_idx)
    for row in rows:
        alert_if_new(row, row, prefs)


def main():
    args = parse_args()
    submitted_debug = os.getenv("WEB_DEBUG_FORM_VALUES")
    db.init_db()
    prefs = load_preferences()

    from scraper.browser import run_login

    # --login: open browser for FB login regardless of credential state
    if args.login:
        asyncio.run(run_login())
        return

    # --cleanup / --tier / --export-only don't need FB credentials
    if args.cleanup:
        db.cleanup_old_posts(config.DATA_RETENTION_DAYS)
        print(f"Cleaned up posts older than {config.DATA_RETENTION_DAYS} days.")
        return

    if args.tier:
        slug = args.tier.replace("-", "_")
        print_tier(slug)
        return

    if args.export_only:
        rows = db.get_listings_with_scores()
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
            "[prefs] "
            f"target_budget={prefs.target_budget} "
            f"max_budget={prefs.max_budget} "
            f"max_move_in_cost={prefs.max_move_in_cost if prefs.max_move_in_cost is not None else 'none'} "
            f"min_size_sqm={prefs.min_size_sqm} "
            f"must_have_washer={prefs.must_have_washer} "
            f"need_parking={prefs.need_parking}"
        )
        print(
            f"[phase] scrape_start groups={len(config.FB_GROUP_URLS)} "
            f"max_posts={args.max_posts or config.MAX_POSTS_PER_RUN} "
            f"vision={'on' if (args.enable_vision or config.ENABLE_VISION) else 'off'}"
        )
        print(f"Starting scrape — max {args.max_posts or config.MAX_POSTS_PER_RUN} posts, "
              f"{len(config.FB_GROUP_URLS)} group(s)")
        stats = await run_scrape(args, prefs)
        print(
            f"[phase] scrape_done groups={stats['groups_scanned']} "
            f"posts={stats['posts_found']} listings={stats['listings_extracted']} "
            f"must_call={stats['must_call_count']}"
        )
        print(f"\nDone — posts: {stats['posts_found']}, "
              f"listings: {stats['listings_extracted']}, "
              f"must_call: {stats['must_call_count']}")
        print("[phase] alerts_start")
        run_alerts(prefs)
        print("[phase] export_start")
        rows = db.get_listings_with_scores()
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
