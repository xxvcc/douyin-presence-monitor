"""Douyin Presence Monitor management CLI (standard library only).

Global options precede subcommands. Runtime/operations imports are intentionally
lazy: --help and configuration inspection never initialize services or storage.
"""
from __future__ import annotations

import argparse
from collections import deque
import datetime as dt
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time

from .config import SAFE_RULES, atomic_write_config, load_config, parse_target, validate_config
from .credentials import (atomic_private_write, prompt_secret, read_secret,
                          require_tty, fsync_directory, ensure_plain_path)
from .management import (RECOVERY_MARKER, RESTORE_MARKER, apply_switch, assert_no_recovery, change_outbox,
                         mutate_config, mutation_lock, private_directory, recover_switch,
                         require_empty_queue, revision)

TEST_MESSAGE = '抖音监测通知通道测试：这不是实际上线、下线或异常事件。'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats unknown argv values; never echo pasted secrets.
        self.print_usage(sys.stderr)
        self.exit(2, 'invalid command arguments; see --help\n')


def positive_int(value):
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError('expected integer in 1..10000')
    return number


def parse_since(value):
    if value is None:
        return None
    if value == 'today':
        now = dt.datetime.now().astimezone()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        date = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            raise ValueError('timezone required')
        return date.timestamp()
    except (ValueError, OverflowError):
        raise ValueError('since requires today or timezone-aware ISO8601') from None


def build_parser():
    p = Parser(description='Douyin Presence Monitor 2 — explicit authorized targets only',
               epilog='Global flags precede subcommands. Secret values are accepted only by hidden TTY prompts.')
    from . import __version__
    p.add_argument('--version', action='version', version=__version__)
    from .paths import installation_root
    root = installation_root()
    p.add_argument('--config', default=os.environ.get('DPM_CONFIG', str(root / 'config/config.json')))
    p.add_argument('--credentials', default=os.environ.get('DPM_CREDENTIALS', str(root / 'config')))
    p.add_argument('--state-dir', default=os.environ.get('DPM_STATE_DIR', str(root / 'data')))
    p.add_argument('--log-dir', default=os.environ.get('DPM_LOG_DIR', str(root / 'logs')))
    p.add_argument('--json', action='store_true', help='machine-readable JSON output')
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='TTY first-run wizard; never overwrite an existing setup')
    run = commands.add_parser('run', help='run collector and durable sender')
    run.add_argument('--once', action='store_true')
    run.add_argument('--no-notify', action='store_true')
    commands.add_parser('probe', help='explicit network identity and target probe; no persistence')
    commands.add_parser('status', help='offline read-only status; never creates or migrates SQLite')
    doctor = commands.add_parser('doctor', help='nonmutating offline checks by default')
    doctor.add_argument('--network', action='store_true', help='explicit identity/API connectivity checks; no messages')
    config = commands.add_parser('config').add_subparsers(dest='action', required=True)
    config.add_parser('show')
    config.add_parser('validate')
    setter = config.add_parser('set', help='safe global rules only; value is JSON')
    setter.add_argument('key', choices=sorted(SAFE_RULES))
    setter.add_argument('value')
    targets = commands.add_parser('targets').add_subparsers(dest='action', required=True)
    targets.add_parser('list')
    add = targets.add_parser('add', help='add one explicitly authorized target; service must be stopped')
    add.add_argument('target')
    add.add_argument('--label', required=True)
    for action in ('pause', 'resume', 'remove'):
        t = targets.add_parser(action, help='service must be stopped; history retained')
        t.add_argument('target')
    logs = commands.add_parser('logs')
    logs.add_argument('--stream', choices=('app', 'error', 'audit'), default='app')
    logs.add_argument('--level', choices=('debug', 'info', 'warning', 'error', 'critical'))
    logs.add_argument('--follow', action='store_true')
    logs.add_argument('--limit', type=positive_int, default=100)
    logs.add_argument('--target')
    logs.add_argument('--since')
    events = commands.add_parser('events')
    events.add_argument('--since')
    events.add_argument('--target')
    events.add_argument('--limit', type=positive_int, default=100)
    notify = commands.add_parser('notify').add_subparsers(dest='action', required=True)
    notify.add_parser('setup', help='hidden TTY bot/route migration; queue must be empty')
    notify.add_parser('test', help='send ONE explicitly labeled test; no presence event is fabricated')
    notify.add_parser('recover', help='stopped-service rollback from interrupted notification switch marker')
    credential = commands.add_parser('credentials').add_subparsers(dest='action', required=True)
    credential.add_parser('update', help='TTY session renewal; verifies SAME observer account')
    backup = commands.add_parser('backup')
    backup.add_argument('--output')
    restore = commands.add_parser('restore')
    restore.add_argument('backup_dir')
    export = commands.add_parser('export')
    export.add_argument('--format', choices=('csv', 'json'), default='csv')
    export.add_argument('--output', required=True)
    export.add_argument('--target')
    export.add_argument('--since')
    outbox = commands.add_parser('outbox').add_subparsers(dest='action', required=True)
    retry = outbox.add_parser('retry', help='requeue an explicit BLOCKED notification on its original route')
    retry.add_argument('id', type=int)
    discard = outbox.add_parser('discard', help='explicitly terminate a BLOCKED notification; history retained')
    discard.add_argument('id', type=int)
    discard.add_argument('--reason', required=True)
    return p


def emit(value, args):
    # JSON is also a readable default; --json uses compact single-line records.
    print(json.dumps(value, ensure_ascii=False, indent=None if args.json else 2, default=str, allow_nan=False), flush=True)


def audit(args, event_name, **fields):
    # Audit failure must not misreport an already committed backup/config action.
    try:
        from .observability import setup_logging
        cfg = load_config(args.config)
        setup_logging(args.log_dir, retention_days=cfg['log_retention_days'],
                      max_bytes=cfg['log_max_bytes'], total_bytes=cfg['log_total_bytes'])
        logging.getLogger('douyin_monitor.audit').info(event_name, extra={'fields': fields})
    except Exception:
        print(json.dumps({'warning': 'audit_log_unavailable', 'operation_committed': True}), file=sys.stderr)


def snapshot(db_path, cfg=None):
    if not db_path.exists():
        return {'initialized': False, 'status': 'uninitialized', 'schema_version': None,
                'targets': {}, 'outbox': {}, 'samples': 0, 'total_samples': 0,
                'runtime': {'heartbeat_at': None, 'started_at': None, 'stopped_at': None,
                            'pid': None, 'status': 'stopped', 'stale': False}}
    from .storage import status_snapshot
    return status_snapshot(db_path, cfg)


def _route_inputs(cfg):
    chat = input('Telegram chat_id' + (f" [{cfg['chat_id']}]" if cfg else '') + ': ')
    thread = input('Thread ID (blank for root chat, never reuse another bot topic): ')
    proxy = input("Telegram HTTP(S) proxy (blank=keep/default direct; '-'=direct): ")
    chat_id = int(chat) if chat else (cfg['chat_id'] if cfg else 0)
    proxy_value = None if proxy == '-' else (proxy or (cfg['telegram_proxy'] if cfg else None))
    return {'chat_id': chat_id, 'thread_id': int(thread) if thread else None, 'telegram_proxy': proxy_value}


def _verified_bot(token, cfg):
    from .transport import TelegramClient
    bot = TelegramClient(token, cfg['telegram_proxy'], timeout=cfg['request_timeout'])
    identity = bot.get_me()
    if identity.get('is_bot') is not True or str(identity.get('id')) != token.split(':', 1)[0]:
        raise ValueError('notification identity mismatch')
    chat = bot.get_chat(cfg['chat_id'])
    if str(chat.get('id')) != str(cfg['chat_id']):
        raise ValueError('notification destination mismatch')
    return bot, str(identity['id'])


def _send_test(bot, cfg):
    from .messages import telegram_payload
    return bot.send(telegram_payload(cfg, TEST_MESSAGE))


def _consent():
    if input('Send ONE clearly labeled notification test now? Type yes to consent: ') != 'yes':
        raise ValueError('notification test not authorized; setup cancelled')


def init_setup(args):
    require_tty()
    config_path = ensure_plain_path(args.config)
    credential_dir = Path(args.credentials)
    session_path, token_path = credential_dir/'douyin_session', credential_dir/'telegram_token'
    if any(p.exists() or p.is_symlink() for p in (config_path, session_path, token_path, Path(args.state_dir)/'monitor.sqlite3')):
        raise ValueError('existing setup refused')
    if input('I am authorized to monitor the explicitly entered target. Type yes: ') != 'yes':
        raise ValueError('authorization confirmation required')
    target = parse_target(input('Authorized target sec_uid or Douyin profile URL: '))
    label = input('Target note/label: ')
    session = prompt_secret('douyin_session')
    from .transport import DouyinClient
    observer = DouyinClient(session, timeout=12).identity()
    token = prompt_secret('telegram_token')
    route = _route_inputs(None)
    cfg = validate_config({'observer_uid': str(observer), 'target': target, 'label': label, **route})
    bot, bot_id = _verified_bot(token, cfg)
    _consent()
    message_id = _send_test(bot, cfg)
    private_directory(config_path.parent)
    private_directory(credential_dir)
    with mutation_lock(args.state_dir):
        assert_no_recovery(args.state_dir)
        if any(p.exists() or p.is_symlink() for p in (config_path, session_path, token_path, Path(args.state_dir)/'monitor.sqlite3')):
            raise ValueError('concurrent setup creation')
        marker = Path(args.state_dir)/'init-recovery.json'
        atomic_private_write(marker, json.dumps({'version': 1, 'action': 'incomplete_init',
                                                'instruction': 'remove only partial setup files after review; rerun init'})+'\n')
        created = []
        try:
            for path, value in ((session_path, session+'\n'), (token_path, token+'\n')):
                created.append(path)  # also clean up a replace whose directory fsync failed
                atomic_private_write(path, value)
            created.append(config_path)
            atomic_write_config(config_path, cfg)
            marker.unlink()
            fsync_directory(marker.parent)
        except BaseException:
            for path in reversed(created):
                path.unlink(missing_ok=True)
                fsync_directory(path.parent)
            marker.unlink()
            fsync_directory(marker.parent)
            raise
    audit(args, 'init', bot_id=bot_id, target=target)
    return {'initialized': True, 'message_id': message_id, 'observer_uid': cfg['observer_uid']}


def notify_setup(args, cfg, db_path):
    require_tty()
    config_revision = revision(args.config)
    token_path = Path(args.credentials)/'telegram_token'
    token_revision = revision(token_path)
    token = prompt_secret('telegram_token')
    updated = validate_config({**cfg, **_route_inputs(cfg)})
    # Do not touch DB, test-send, or backup if a running collector owns the lock.
    with mutation_lock(args.state_dir):
        assert_no_recovery(args.state_dir)
        if revision(args.config) != config_revision or revision(token_path) != token_revision:
            raise ValueError('concurrent configuration modification')
        if not db_path.is_file():
            raise ValueError('notification migration requires an initialized database')
        from .storage import Store
        from .maintenance import create_backup
        store = Store(db_path)
        try:
            require_empty_queue(store.db)
            store.bind(cfg)
            store.bind_notification_bot(read_secret(token_path).split(':', 1)[0])
            bot, bot_id = _verified_bot(token, updated)
            _consent()
            message_id = _send_test(bot, updated)
            backup = create_backup(args.config, db_path, Path(args.state_dir).parent/'backups')
            result = apply_switch(args.config, token_path, store, updated, token, bot_id,
                                  state_dir=args.state_dir, expected_config=config_revision,
                                  expected_token=token_revision)
        finally:
            store.close()
    audit(args, 'notify_setup', bot_id=bot_id, chat_id=updated['chat_id'], thread_id=updated['thread_id'])
    return {**result, 'backup': str(backup), 'message_id': message_id}


def update_credentials(args, cfg):
    require_tty()
    expected_config = revision(args.config)
    path = Path(args.credentials)/'douyin_session'
    expected_secret = revision(path)
    session = prompt_secret('douyin_session')
    from .transport import DouyinClient
    DouyinClient(session, timeout=cfg['request_timeout']).verify_identity(cfg['observer_uid'])
    with mutation_lock(args.state_dir):
        assert_no_recovery(args.state_dir)
        if revision(args.config) != expected_config or revision(path) != expected_secret:
            raise ValueError('concurrent configuration modification')
        atomic_private_write(path, session+'\n')
    audit(args, 'credentials_update', observer_uid=cfg['observer_uid'])
    return {'updated': True, 'observer_verified': True}


def _target_change(args, cfg):
    identifier = parse_target(args.target)
    found = [t for t in cfg['targets'] if t['target'] == identifier]
    if args.action == 'add':
        if found:
            raise ValueError('target already exists')
        cfg['targets'].append({'target': identifier, 'label': args.label, 'enabled': True})
    else:
        if not found:
            raise ValueError('target not configured')
        if args.action == 'remove':
            cfg['targets'] = [t for t in cfg['targets'] if t['target'] != identifier]
        else:
            found[0]['enabled'] = args.action == 'resume'
    return cfg


def _logs(args):
    from .observability import read_logs
    target = parse_target(args.target) if args.target else None
    since = parse_since(args.since)
    seen = deque(maxlen=10000)
    while True:
        rows = read_logs(args.log_dir, stream=args.stream, level=args.level.upper() if args.level else None,
                         target=target, since=since, limit=args.limit)
        if not args.follow:
            return rows
        previous = set(seen)
        for row in rows:
            key = json.dumps(row, sort_keys=True, ensure_ascii=False)
            if key not in previous:
                emit(row, args)
                seen.append(key)
        time.sleep(1)


def guard_export_destination(args):
    """An export is never a way to overwrite control files or replace a live lock."""
    from .paths import installation_root
    output = Path(args.output).resolve()
    protected_files = [Path(args.config), Path(args.credentials)/'douyin_session',
                       Path(args.credentials)/'telegram_token']
    if output in {p.resolve() for p in protected_files}:
        raise ValueError('export destination is a protected application file')
    for directory in (Path(args.state_dir), Path(args.log_dir), Path(args.state_dir).parent/'backups'):
        directory = directory.resolve()
        if output == directory or directory in output.parents:
            raise ValueError('export destination is a protected application directory')
    root = installation_root().resolve()
    if output == root:
        raise ValueError('export destination must be a new file')
    if root in output.parents and output.relative_to(root).parts[0] != 'exports':
        raise ValueError('use the installation exports directory or an explicit external destination')


def dispatch(args):
    from .paths import managed_path
    args.state_dir = str(managed_path(args.state_dir))
    args.log_dir = str(managed_path(args.log_dir))
    config_path = Path(args.config)
    db_path = Path(args.state_dir)/'monitor.sqlite3'
    command = args.command
    if command == 'init':
        return init_setup(args)
    if command == 'logs':
        return _logs(args)
    if command == 'events':
        from .storage import list_events
        return list_events(db_path, target=parse_target(args.target) if args.target else None,
                           since=parse_since(args.since), limit=args.limit)
    if command == 'export':
        from .maintenance import export_data
        guard_export_destination(args)
        return export_data(db_path, args.output, format=args.format,
                           target=parse_target(args.target) if args.target else None, since=parse_since(args.since))
    if command == 'restore':
        from .maintenance import restore_backup, verify_backup
        from .management import prepare_restore
        assert_no_recovery(args.state_dir, allow_restore=True)
        verify_backup(args.backup_dir)
        prepare_restore(config_path, db_path, args.state_dir)
        result = restore_backup(args.backup_dir, config_path, db_path, Path(args.state_dir)/'monitor.lock')
        audit(args, 'restore', backup=str(args.backup_dir))
        return {'result': result, 'credentials_must_match_restored_binding': True}
    if command == 'notify' and args.action == 'recover':
        with mutation_lock(args.state_dir):
            # mode=rw deliberately refuses creating a replacement empty database.
            db = sqlite3.connect(ensure_plain_path(db_path).as_uri()+'?mode=rw', uri=True)
            try:
                result = recover_switch(config_path, Path(args.credentials)/'telegram_token', db, state_dir=args.state_dir)
            finally:
                db.close()
        audit(args, 'notify_recover')
        return result
    cfg = load_config(config_path)
    if command == 'status':
        return snapshot(db_path, cfg)
    if command == 'doctor':
        result = {'config_valid': True, 'network_checked': False, 'database': snapshot(db_path, cfg),
                  'recovery_pending': any((Path(args.state_dir)/name).exists() for name in (RECOVERY_MARKER, RESTORE_MARKER, 'init-recovery.json', 'restore-recovery.json'))}
        if args.network:
            from .transport import DouyinClient
            DouyinClient(read_secret(Path(args.credentials)/'douyin_session'), timeout=cfg['request_timeout']).verify_identity(cfg['observer_uid'])
            _, bot_id = _verified_bot(read_secret(Path(args.credentials)/'telegram_token'), cfg)
            result.update(network_checked=True, observer_verified=True, bot_id=bot_id)
        return result
    if command == 'run':
        assert_no_recovery(args.state_dir)
        from .runtime import run
        return run(cfg, args.credentials, args.state_dir, args.log_dir, once=args.once,
                   no_notify=args.no_notify, config_path=args.config)
    if command == 'probe':
        from .runtime import probe
        return probe(cfg, args.credentials)
    if command == 'config':
        if args.action == 'show':
            return cfg
        if args.action == 'validate':
            return {'valid': True, 'schema_version': cfg['schema_version'], 'targets': len(cfg['targets'])}
        expected = revision(config_path)
        value = json.loads(args.value)
        result = mutate_config(config_path, args.state_dir, lambda c: {**c, args.key: value}, expected)
        audit(args, 'config_set', key=args.key, value=result[args.key])
        return {'updated': args.key, 'value': result[args.key]}
    if command == 'targets':
        if args.action == 'list':
            return cfg['targets']
        result = mutate_config(config_path, args.state_dir, lambda c: _target_change(args, c), revision(config_path))
        audit(args, 'target_'+args.action, target=parse_target(args.target))
        return {'targets': result['targets'], 'history_retained': True}
    if command == 'credentials':
        return update_credentials(args, cfg)
    if command == 'notify':
        if args.action == 'setup':
            return notify_setup(args, cfg, db_path)
        assert_no_recovery(args.state_dir)
        bot, bot_id = _verified_bot(read_secret(Path(args.credentials)/'telegram_token'), cfg)
        message_id = _send_test(bot, cfg)
        audit(args, 'notify_test', bot_id=bot_id, message_id=message_id)
        return {'test': True, 'message_id': message_id, 'presence_event_created': False}
    if command == 'backup':
        from .maintenance import create_backup
        result = create_backup(config_path, db_path, args.output or Path(args.state_dir).parent/'backups')
        audit(args, 'backup', output=str(result))
        return {'backup': str(result), 'includes_credentials': False}
    if command == 'outbox':
        with mutation_lock(args.state_dir):
            assert_no_recovery(args.state_dir)
            from .storage import status_snapshot
            status_snapshot(db_path, cfg)  # reject unrelated/newer schemas before any write
            db = sqlite3.connect(ensure_plain_path(db_path).as_uri()+'?mode=rw', uri=True)
            try:
                result = change_outbox(db, args.id, args.action, getattr(args, 'reason', None))
            finally:
                db.close()
        audit(args, 'outbox_'+args.action, **result)
        return result
    raise ValueError('unknown command')


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_request:
        return int(exit_request.code or 0)
    except (OSError, ValueError):
        print(json.dumps({'error': 'invalid_installation_directory'}), file=sys.stderr)
        return 1
    try:
        result = dispatch(args)
        if args.command == 'run' and type(result) is int:
            return result
        emit(result, args)
        return 0
    except (KeyboardInterrupt, EOFError):
        print(json.dumps({'error': 'cancelled'}), file=sys.stderr)
        return 130
    except Exception:
        # Do not expose raw transport URLs, credentials, sqlite statements, or
        # arbitrary exception messages. Operational logs have their own redactor.
        print(json.dumps({'error': 'operation_failed', 'hint': 'Check configuration, permissions, stopped-service lock and recovery markers. No automatic test-message retry.'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
