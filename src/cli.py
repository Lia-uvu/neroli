#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import shlex
from pathlib import Path

from config import MEMORY, PROJECT_DIRS, ROOMS, custom_cli_cmd, load_settings, model_provider
from context import rebuild_context
from db import (
    DB,
    connect,
    create_pipeline_run,
    get_session_ids,
    ingest_conversation_tree,
    ingest_turns,
    reconcile_deleted_files,
    refresh_session_forks,
)
from loaders import load_sources_for_ingest, message_to_dict
from model import build_model
from pipeline import auto_generate_cards, finalize_card_updates, process_session, rebuild_index, reroom_cards


# 调用方式跟 settings.model_access.provider 走；显式 --provider 照旧覆盖。
DEFAULT_PROVIDER = model_provider()


def default_model() -> str:
    """未显式给 --model 时的模型：settings.card_gen.model。"""
    return load_settings().get("card_gen", {}).get("model", "gpt-5.4-mini")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="recall-pipeline: ingest transcripts, build factual memory.")
    parser.add_argument("inputs", nargs="*", type=Path, help="Claude JSONL, normalized JSON, or test txt files.")
    parser.add_argument("--provider", choices=["ollama", "api", "cli"], default=DEFAULT_PROVIDER, help="Model provider (default: cli).")
    parser.add_argument("--model", default=None, help="Model name for the selected provider.")
    parser.add_argument("--model-cmd", default=None, help="Full command line for --provider cli.")
    parser.add_argument("--api-base-url", default=None, help="OpenAI-compatible API base URL.")
    parser.add_argument("--api-key-env", default="CLAUDE_MEMORY_API_KEY", help="Environment variable containing the API key.")
    parser.add_argument("--api-temperature", type=float, default=0.0, help="Temperature for API calls.")
    parser.add_argument("--api-max-tokens", type=int, default=None, help="Optional max_tokens for API calls.")
    parser.add_argument("--api-timeout", type=float, default=240.0, help="API timeout in seconds.")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--max-messages", type=int, default=80, help="Tail limit for --dump-json convenience only; use 0 for all. Ingest always reads full files.")
    parser.add_argument("--rebuild-cards", "--rebuild-context", dest="rebuild_context",
                        action="store_true",
                        help="Only rewrite <room>/cards-last-24.md from existing DB.")
    parser.add_argument("--rebuild-index", action="store_true", help="Only rebuild the card index from existing cards.")
    parser.add_argument("--rebuild-forks", action="store_true", help="Only rebuild inferred session fork relationships.")
    parser.add_argument("--reroom-cards", action="store_true", help="Re-derive each card's room from its session source file and rebuild context.")
    parser.add_argument("--list-forks", action="store_true", help="List inferred session fork relationships.")
    parser.add_argument("--render-history", action="store_true", help="Print source-neutral conversation trees and latest render observations as JSON.")
    parser.add_argument("--history-room", default=None, help=f"Room for --render-history ({'/'.join(ROOMS)}).")
    parser.add_argument("--history-source", default=None, help="Optional adapter source filter for --render-history.")
    parser.add_argument("--history-context", default=None, help="Optional native context/session filter for --render-history.")
    parser.add_argument("--dump-json", type=Path, help="Normalize inputs to JSON, then exit.")
    parser.add_argument("--ingest-only", action="store_true", help="Write input turns to SQLite without running the model.")
    parser.add_argument("--process-existing", action="store_true", help="Run the model from turns already in SQLite without reading input files.")
    parser.add_argument("--session-id", action="append", help="Limit processing to one session id. Can be passed more than once.")
    parser.add_argument("--session-file", type=Path, help="Read session ids from a text file.")
    parser.add_argument("--skip-processed", action="store_true", help="Skip sessions that already have cards.")
    parser.add_argument("--process-yesterday", action="store_true", help="Card the last day of already-ingested turns (first-run convenience; implies --process-existing --skip-processed).")
    parser.add_argument("--dry-run", action="store_true", help="Print selected sessions and exit.")
    parser.add_argument("--viewer", dest="context_room", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--context-room", dest="context_room", default=None, help=f"Rebuild context for one room only ({'/'.join(ROOMS)}).")
    parser.add_argument("--room", default=None, help=f"Force room tag for generated cards ({'/'.join(ROOMS)}). Default: auto-detect per session from source file.")
    parser.add_argument("--auto-cards", action="store_true", help="Check dual threshold and generate cards if conditions met.")
    parser.add_argument("--summarize-last24", action="store_true",
                        help="Rebuild cards-last-24 and use an agentic CLI + recall to write summary-last-24.")
    parser.add_argument("--last24-export-workbench", action="store_true",
                        help="Rebuild cards-last-24 and export last-24 workbenches without calling a model.")
    parser.add_argument("--curate", action="store_true", help="Midlayer full nightly: snapshot + per-room curator agent (digest + constants). Needs a model.")
    parser.add_argument("--curate-snapshot", action="store_true", help="Midlayer: snapshot tonight's cluster tree into tree_snapshots (no model).")
    parser.add_argument("--curate-dry-run", action="store_true", help="Midlayer: print the tree-change report without calling the model (tune thresholds).")
    parser.add_argument("--curate-export-workbench", action="store_true", help="Midlayer: export per-room viewer-filtered workbenches (no model).")
    parser.add_argument("--night", default=None, help="Override the night (local date) for --curate-* commands.")
    return parser


def run(argv: list[str] | None = None) -> int:
    load_env_file(MEMORY / ".env")
    args = build_parser().parse_args(argv)
    if args.process_yesterday:
        args.process_existing = True
        args.skip_processed = True
    inputs = [] if args.process_existing else (args.inputs or default_inputs())
    has_explicit_inputs = bool(args.inputs)
    # 扫描模式 = watcher 那种「不给输入、不指定 session、不 process-existing」的全房间重读。
    # 只有它才做增量文件选择；显式输入 / 指定 session 的手动调用照旧全读。
    scan_mode = not (args.process_existing or args.inputs or args.session_id or args.session_file)

    if args.dump_json:
        # 与 ingest 同一条加载路径（全量读 + 跨文件去重 + 统一编号），dump 才能和入库一致。
        # --max-messages 仅作 dump 时的查看便利，在聚合编号之后再截尾。
        loaded_sources = load_sources_for_ingest(inputs)
        loaded = list(loaded_sources.messages)
        if args.max_messages > 0:
            loaded = loaded[-args.max_messages:]
        messages = [message_to_dict(msg) for msg in loaded]
        payload: object = messages
        if loaded_sources.conversation_trees:
            payload = {
                "messages": messages,
                "conversation_trees": [asdict(batch) for batch in loaded_sources.conversation_trees],
            }
        args.dump_json.parent.mkdir(parents=True, exist_ok=True)
        args.dump_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.dump_json}")
        return 0

    conn = connect(args.db)

    if args.render_history:
        if not args.history_room:
            raise ValueError("--render-history requires --history-room")
        from history import render_history
        print(json.dumps(render_history(
            conn,
            room=args.history_room,
            source=args.history_source,
            native_context_id=args.history_context,
        ), ensure_ascii=False, indent=2))
        return 0

    if args.rebuild_context:
        rebuild_context(conn, viewer=args.context_room)
        print("wrote <room>/cards-last-24.md")
        return 0

    if args.rebuild_index:
        stats = rebuild_index(conn)
        print(f"rebuilt index: {stats}")
        return 0

    if args.rebuild_forks:
        n = refresh_session_forks(conn)
        conn.commit()
        print(f"rebuilt forks: {n}")
        return 0

    if args.reroom_cards:
        stats = reroom_cards(conn)
        rebuild_context(conn, viewer=args.context_room)
        print(f"rerooted cards: {stats}")
        print("wrote <room>/cards-last-24.md")
        return 0

    if args.list_forks:
        rows = conn.execute(
            """
            SELECT child_session_id, parent_session_id, fork_round,
                   delta_start_round, shared_turns, child_turns,
                   parent_turns, child_shared_ratio
            FROM session_forks
            ORDER BY detected_at DESC, child_session_id
            """
        ).fetchall()
        for r in rows:
            ratio = f"{r['child_shared_ratio']:.2f}"
            print(
                f"{r['child_session_id'][:8]} <- {r['parent_session_id'][:8]} "
                f"fork=R{r['fork_round']} delta=R{r['delta_start_round']} "
                f"shared={r['shared_turns']}/{r['child_turns']} ratio={ratio}"
            )
        print(f"forks: {len(rows)}")
        return 0

    if args.auto_cards:
        s = load_settings().get("card_gen", {})
        from pipeline import check_card_gen_threshold
        if args.dry_run:
            # 与 auto_generate_cards 同一套阈值+选点，不建模型、不落 pipeline_run。
            from pipeline import sessions_needing_update
            ok, reason = check_card_gen_threshold(conn)
            if not ok:
                print(f"auto-cards dry-run: not triggered ({reason})")
                return 0
            targets = sessions_needing_update(conn)[: s.get("max_sessions_per_trigger", 3)]
            for session_id, room in targets:
                print(f"{session_id} [{room}]")
            print(f"auto-cards dry-run: would update {len(targets)} sessions")
            return 0
        ok, reason = check_card_gen_threshold(conn)
        if not ok:
            print(f"auto-cards: not triggered ({reason})")
            return 0
        model_name = args.model or default_model()
        if args.provider == "cli" and not args.model_cmd:
            effort = s.get("reasoning_effort", "low")
            from model import _build_codex_cmd_with_effort
            args.model_cmd = custom_cli_cmd(model_name) or _build_codex_cmd_with_effort(model_name, effort)
        model = make_model(args.provider, model_name, args)
        run_id = create_pipeline_run(conn, model.name, None, [])
        n = auto_generate_cards(conn, run_id, model)
        if n:
            print(f"auto-cards: updated {n} sessions")
            run_last24_summary(conn, args, viewer=args.context_room)
        return 0

    if args.summarize_last24 or args.last24_export_workbench:
        rebuild_context(conn, viewer=args.context_room)
        print("wrote <room>/cards-last-24.md")
        import last24
        if args.last24_export_workbench:
            rooms = last24.enabled_rooms(args.context_room)
            for room in rooms:
                print(f"workbench: {last24.export_workbench(conn, room, args.db)}")
            return 0
        run_last24_summary(conn, args, viewer=args.context_room)
        return 0

    if args.curate or args.curate_snapshot or args.curate_dry_run or args.curate_export_workbench:
        # 中期层（夜间 curator）。midlayer.enabled=false 时整体跳过（同 index.enabled 样式）。
        settings = load_settings()
        if not settings.get("midlayer", {}).get("enabled", True):
            print("curate skipped (settings.midlayer.enabled=false)")
            return 0
        import treesnap
        night = args.night or treesnap.tonight_night()
        if args.curate:
            # 全流程（有模型成本）：快照 → 每房间导出工作台 + agent 调用 → 落 digest/constants。
            import curator
            from model import _build_codex_cmd_with_effort
            ms = settings.get("midlayer", {})
            model_name = args.model or ms.get("model", default_model())
            model_cmd = args.model_cmd
            if args.provider == "cli" and not model_cmd:
                # curator 需要在工作台里跑 ./submit 落 submission.json → workspace-write。
                # 自定义 cli_cmd 时沙盒由用户的命令自己负责：须是能写 cwd 的 agentic CLI。
                model_cmd = custom_cli_cmd(model_name) or _build_codex_cmd_with_effort(
                    model_name, ms.get("reasoning_effort", "low"), sandbox="workspace-write")
            timeout = float(ms.get("timeout_seconds", 900))
            treesnap.snapshot(conn, night=night)  # 先落今夜快照，diff 才有今夜可比

            def make_curator_model(cwd: str):
                return build_model(
                    args.provider, model_name,
                    api_base_url=args.api_base_url or os.environ.get("CLAUDE_MEMORY_API_BASE_URL"),
                    api_key_env=args.api_key_env, temperature=args.api_temperature,
                    max_tokens=args.api_max_tokens, timeout=timeout,
                    model_cmd=model_cmd, cwd=cwd, success_artifact="submission.json",
                )

            results = curator.run_curation(conn, make_curator_model, night=night, db_path=args.db)
            if not results:
                print("no rooms enabled for curation (settings.midlayer.rooms)")
            for r in results:
                if r.get("skipped"):
                    print(f"curate {r['room']}: skipped ({r['skipped']})")
                    continue
                via = "submit" if r.get("submitted") else "stdout-fallback"
                print(f"curate {r['room']}: digest {r['digest_chars']}字 [{via}], constants {r['constants']}")
            return 0
        if args.curate_snapshot:
            print(f"snapshot: {treesnap.snapshot(conn, night=night)}")
        if args.curate_dry_run:
            print(treesnap.render_report(conn, night=night, viewer=args.context_room))
        if args.curate_export_workbench:
            import curator
            dests = curator.export_all_workbenches(conn, night=night, db_path=args.db)
            for d in dests:
                print(f"workbench: {d}")
            if not dests:
                print("no rooms enabled for curation (settings.midlayer.rooms)")
        return 0

    session_ids: list[str] = args.session_id or []
    if args.session_file:
        session_ids.extend(read_session_file(args.session_file))
    if args.process_yesterday:
        rows = conn.execute(
            """
            SELECT DISTINCT t.session_id FROM turns t
            JOIN messages m ON m.source_uuid = t.source_uuid
            WHERE substr(m.timestamp,1,10) >= date('now','-1 day')
            """
        ).fetchall()
        session_ids.extend(r["session_id"] for r in rows)
        # 空结果必须在这里退出：漏过这行会掉进下面「无 session 则全量」的兜底，把全库都拿去出卡。
        if not session_ids:
            print("no sessions with turns in the last day; nothing to card")
            return 0

    # 增量：扫描模式下只把 mtime 变过的文件（及与之共享 session 的文件）喂给 ingest。
    # 每个被触碰的 session 仍从它的全部文件一起加载，round 跨文件一次性算不受影响。
    state_path = None
    new_mtimes = None
    if scan_mode and inputs:
        if load_settings().get("watcher", {}).get("incremental_ingest", True):
            from incremental import load_state, save_state, select_incremental, state_path_for
            state_path = state_path_for(args.db)
            inputs, new_mtimes = select_incremental(conn, inputs, load_state(state_path))

    if inputs:
        # ingest 必须读完整文件：round 编号跨文件一次性算，截断会让轮次漂移。
        # --max-messages 不影响 ingest（出卡步在 generate() 里自己开窗），只用于 --dump-json。
        loaded = load_sources_for_ingest(inputs)
        all_messages = list(loaded.messages)
        new_session_ids = ingest_turns(conn, all_messages) if all_messages else []
        for tree in loaded.conversation_trees:
            new_session_ids.extend(ingest_conversation_tree(conn, tree))
        new_session_ids = sorted(set(new_session_ids))
        if not session_ids:
            session_ids = new_session_ids
        else:
            session_ids = sorted(set(session_ids) & set(new_session_ids))
        tree_nodes = sum(len(tree.nodes) for tree in loaded.conversation_trees)
        observations = sum(len(tree.observations) for tree in loaded.conversation_trees)
        print(
            f"loaded {len(all_messages)} messages + {tree_nodes} tree nodes + "
            f"{observations} observations; sessions with new turns: {len(session_ids)}"
        )
    elif new_mtimes is not None:
        print("no changed files since last ingest")

    # ingest 成功后才推进状态：中途异常则不写，下次仍会重读那些变化文件（幂等 upsert）。
    if new_mtimes is not None:
        save_state(state_path, new_mtimes)

    pruned = reconcile_deleted_files(conn, PROJECT_DIRS)
    if pruned:
        refresh_session_forks(conn)
        conn.commit()
        print(f"reconcile: removed {pruned} turns from deleted source files")

    if args.ingest_only:
        print(f"wrote {args.db}")
        return 0

    if not session_ids:
        if has_explicit_inputs:
            print("no new turns ingested")
            rebuild_context(conn, viewer=args.context_room)
            print(f"wrote {args.db}")
            return 0
        session_ids = get_session_ids(conn)
    if not session_ids:
        print("no turns found")
        return 1
    if args.skip_processed:
        before = len(session_ids)
        session_ids = skip_processed_sessions(conn, session_ids)
        print(f"skipped {before - len(session_ids)} already processed sessions; remaining: {len(session_ids)}")
    if not session_ids:
        print("no selected sessions to process")
        rebuild_context(conn, viewer=args.context_room)
        return 0
    if args.dry_run:
        for session_id in session_ids:
            print(session_id)
        return 0

    # 主处理路径（补漏出卡等）：模型与 reasoning effort 都跟 settings.card_gen 走，
    # 和 --auto-cards 同一套默认；显式 --model / --model-cmd 照旧覆盖。
    s = load_settings().get("card_gen", {})
    model_name = args.model or default_model()
    if args.provider == "cli" and not args.model_cmd:
        from model import _build_codex_cmd_with_effort
        args.model_cmd = custom_cli_cmd(model_name) or _build_codex_cmd_with_effort(model_name, s.get("reasoning_effort", "low"))
    model = make_model(args.provider, model_name, args)
    run_id = create_pipeline_run(conn, model.name, None, inputs)

    failed = []
    changed = False
    for idx, session_id in enumerate(session_ids, 1):
        try:
            changed = process_session(conn, run_id, session_id, model, room=args.room) or changed
            conn.commit()
        except Exception as exc:
            conn.rollback()
            failed.append(session_id)
            print(f"[{idx}/{len(session_ids)}] FAILED {session_id}: {exc}")
        else:
            print(f"[{idx}/{len(session_ids)}] {session_id}")
    if failed:
        print(f"\n{len(failed)} sessions failed (timeout/API error)")

    if changed:
        index_stats = finalize_card_updates(conn)
        run_last24_summary(conn, args, viewer=args.context_room)
    else:
        rebuild_context(conn)
        index_stats = {"status": "skipped", "reason": "no card changes"}
    print(f"wrote {args.db}")
    print(f"rebuilt index: {index_stats}")
    print("wrote <room>/cards-last-24.md")
    return 1 if failed else 0


def run_last24_summary(conn, args: argparse.Namespace, viewer: str | None = None) -> list[dict]:
    """Build the dedicated Sol exec used for recent summaries."""
    import last24
    settings = load_settings()
    cfg = settings.get("last24_summary", {})
    if not cfg.get("enabled", True):
        print("last24 summary skipped (settings.last24_summary.enabled=false)")
        return []
    if args.provider != "cli":
        raise RuntimeError("last24 summary requires provider=cli so recall and ./submit can run")
    from model import _build_codex_cmd_with_effort
    direct = bool(args.summarize_last24)
    model_name = (args.model if direct else None) or cfg.get("model", "gpt-5.6-sol")
    model_cmd = (args.model_cmd if direct else None) or custom_cli_cmd(model_name) or _build_codex_cmd_with_effort(
        model_name, cfg.get("reasoning_effort", "medium"), sandbox="workspace-write")
    timeout = float(cfg.get("timeout_seconds", 900))

    def model_factory(cwd: str):
        return build_model(
            "cli", model_name, timeout=timeout, model_cmd=model_cmd, cwd=cwd,
            success_artifact="submission.json")

    results = last24.run_summaries(conn, model_factory, viewer=viewer, db_path=args.db)
    for result in results:
        via = "no-model" if not result["model"] else (
            "submit" if result.get("submitted") else "stdout-fallback")
        print(f"last24 {result['room']}: {result['summary_chars']}字 [{via}]")
    return results


def make_model(provider: str, model_name: str, args: argparse.Namespace):
    api_base_url = args.api_base_url
    if api_base_url is None:
        api_base_url = os.environ.get("CLAUDE_MEMORY_API_BASE_URL")
    return build_model(
        provider,
        model_name,
        api_base_url=api_base_url,
        api_key_env=args.api_key_env,
        temperature=args.api_temperature,
        max_tokens=args.api_max_tokens,
        timeout=args.api_timeout,
        model_cmd=args.model_cmd,
    )


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
        except ValueError:
            parsed = []
        os.environ[key] = parsed[0] if parsed else value.strip().strip("\"'")


def default_inputs() -> list[Path]:
    paths: list[Path] = []
    for project_dir in PROJECT_DIRS:
        if project_dir.exists():
            paths.extend(project_dir.glob("*.jsonl"))
    return sorted(paths, key=lambda path: path.stat().st_mtime)


def read_session_file(path: Path) -> list[str]:
    session_ids: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        session_ids.append(line.split()[0])
    return session_ids


def skip_processed_sessions(conn, session_ids: list[str]) -> list[str]:
    if not session_ids:
        return []
    placeholders = ",".join("?" for _ in session_ids)
    processed = {
        row["session_id"]
        for row in conn.execute(
            f"SELECT DISTINCT session_id FROM cards WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
    }
    return [session_id for session_id in session_ids if session_id not in processed]


if __name__ == "__main__":
    raise SystemExit(run())
