"""Sequential per-target collector, independent durable notifier and heartbeat."""
from __future__ import annotations
import dataclasses
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time

from .domain import Problem, Observation, State, advance, retry_delay
from .locking import InstanceLock
from .storage import Store
from .transport import DouyinClient, TelegramClient

ACCOUNT_REASONS = {'login_expired', 'account_mismatch', 'identity_unverified'}
HEARTBEAT_SECONDS = 15
REQUEST_SPACING = 1.0
logger = logging.getLogger('douyin_monitor')


def _log(message, *, level=logging.INFO, **fields):
    logger.log(level, message, extra={'fields': fields})


def _secret(path):
    # Parallel-owned dependency is resolved only at the execution boundary.
    from .credentials import read_secret
    return read_secret(path)


def _session(credentials_dir):
    token = _secret(Path(credentials_dir) / 'douyin_session')
    if not re.fullmatch(r'[0-9a-fA-F]{32}', token):
        raise ValueError('invalid session credential')
    return token


def _bot_token(credentials_dir):
    token = _secret(Path(credentials_dir) / 'telegram_token')
    if not re.fullmatch(r'[1-9][0-9]*:[A-Za-z0-9_-]+', token):
        raise ValueError('invalid notification credential')
    return token


def _heartbeat(path, stop):
    db = Store(path)
    try:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                db.runtime_metadata(heartbeat_at=time.time())
            except Exception:
                logger.warning('heartbeat_write_failed', extra={'fields': {'reason': 'storage_error'}})
    finally:
        db.close()


def sender_loop(path, cfg, credentials_dir, stop, fatal):
    """Single owner of delivery; failures never block the sampling thread."""
    db = Store(path)
    client = None
    bot_id = None
    try:
        while not stop.is_set():
            try:
                if client is None:
                    if db.notification_sender().get('status') == 'blocked' and not db.sender_retry_requested():
                        stop.wait(1)
                        continue
                    token = _bot_token(credentials_dir)
                    credential_bot = token.split(':', 1)[0]
                    try:
                        db.check_notification_bot(credential_bot)
                    except ValueError:
                        fatal.set(); stop.set()
                        _log('notification_binding_mismatch', level=logging.ERROR)
                        break
                    candidate = TelegramClient(token, cfg.get('telegram_proxy'), timeout=cfg.get('request_timeout', 12))
                    try:
                        identity = candidate.get_me()
                    except Problem as error:
                        if not error.permanent:
                            raise
                        db.block_notification_sender(credential_bot, error, time.time())
                        _log('notification_sender_blocked', level=logging.ERROR,
                             reason='sender_auth_permanent', error=error.code, detail=error.detail)
                        stop.wait(1)
                        continue
                    bot_id = str(identity['id'])
                    if bot_id != token.split(':', 1)[0]:
                        fatal.set(); stop.set()
                        _log('notification_binding_mismatch')
                        break
                    try:
                        db.bind_notification_bot(bot_id)
                    except ValueError:
                        fatal.set(); stop.set()
                        _log('notification_binding_mismatch')
                        break
                    client = candidate
                rows = db.pending(time.time())
                if not rows:
                    stop.wait(0.25)
                    continue
                row = rows[0]
                if row['bot_id'] != bot_id:
                    fatal.set(); stop.set()
                    _log('notification_binding_mismatch')
                    break
                try:
                    message_id = client.send(json.loads(row['payload']))
                    db.mark_sent(row['id'], message_id, time.time())
                    _log('telegram_accepted', outbox_id=row['id'], message_id=message_id)
                except Problem as error:
                    delay = min(3600, max(error.retry_after, 30 * 2 ** min(row['attempts'], 7)))
                    db.mark_failed(row['id'], error.code, time.time(), delay, permanent=error.permanent)
                    _log('notification_blocked' if error.permanent else 'notification_retry', level=logging.ERROR if error.permanent else logging.WARNING, outbox_id=row['id'], reason=error.code, delay=delay)
            except Exception:
                # Do not log raw exception, token-bearing URL, request, or response.
                logger.warning('notification_worker_unavailable', extra={'fields': {'reason': 'notification_error'}})
                client = None
                stop.wait(30)
    finally:
        db.close()


def _delay(state, cfg, problem):
    if state.mode != 'unknown':
        state.next_delay = cfg['poll_seconds']
    else:
        count = state.timeout_streak if state.reason == 'network_timeout' else state.failures
        state.next_delay = retry_delay(state.reason, count, cfg['poll_seconds'],
                                       problem.retry_after if problem else 0,
                                       timeout_alert_after=cfg['timeout_alert_after'])


def _advance(state, observation, problem, now, cfg, *, notify_allowed=True):
    events = advance(state, observation, problem, now, cfg['offline_after_seconds'],
                     cfg['unknown_reason_cooldown'], max_gap=max(90, cfg['poll_seconds'] * 3),
                     timeout_alert_after=cfg['timeout_alert_after'], notify_allowed=notify_allowed)
    _delay(state, cfg, problem)
    return events


def _sample_record(state, observation, now):
    return {'observed_at': now, 'mode': state.mode, 'reason': state.reason,
            'returned_active_at': observation.active_at if observation else None,
            'server_at': observation.server_at if observation else None,
            'next_delay': state.next_delay, 'timeout_streak': state.timeout_streak,
            'unknown_alerted': state.unknown_alerted}


def run(cfg, credentials_dir, state_dir, log_dir, *, once=False, no_notify=False, config_path=None):
    from .observability import setup_logging
    from .paths import managed_path
    from .management import assert_no_recovery
    from . import __version__
    state_dir = managed_path(state_dir)
    log_dir = managed_path(log_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_dir / 'monitor.sqlite3'
    with InstanceLock(state_dir / 'monitor.lock'):
        assert_no_recovery(state_dir)
        if config_path is not None:
            from .config import load_config
            cfg = load_config(config_path)
        session = _session(credentials_dir)
        token = None
        try:
            token = _bot_token(credentials_dir)
        except (OSError, ValueError):
            pass
        setup_logging(log_dir, secrets=tuple(v for v in (session, token) if v),
                      retention_days=cfg.get('log_retention_days', 14), max_bytes=cfg.get('log_max_bytes', 10485760),
                      total_bytes=cfg.get('log_total_bytes', 104857600))
        db = Store(path)
        stop, fatal = threading.Event(), threading.Event()
        threads, previous_handlers = [], {}
        started = False
        try:
            db.bind(cfg)
            # Reject known credential switches before any collection, even if getMe is down.
            if not no_notify and token:
                db.check_notification_bot(token.split(':', 1)[0])
            client = DouyinClient(session, timeout=cfg.get('request_timeout', 12))
            targets = [t for t in cfg['targets'] if t['enabled']]
            states = {t['target']: db.load(t['target']) for t in targets}
            for state in states.values():
                state.reset_confidence()
                if state.reason in ACCOUNT_REASONS:
                    # Legacy migration copied the shared login episode into target
                    # state; it never represented a separate target notification.
                    state.unknown_alerted = False
                    state.notified_reason = ''
                    state.notified_at = 0
            now = time.time()
            due = {target: now if once else max(now, s.last_attempt_at + s.next_delay) for target, s in states.items()}
            account = db.load_account()
            account.reset_confidence()
            identity_until = 0
            account_due = now if once or account.mode != 'unknown' else max(now, account.last_attempt_at + account.next_delay)
            last_request = 0.0
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGTERM, signal.SIGINT):
                    previous_handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, lambda *_: stop.set())
            db.runtime_metadata(heartbeat_at=now, started_at=now, stopped_at=None, pid=os.getpid(), status='running')
            started = True
            _log('service_started', version=__version__, pid=os.getpid(), targets=len(targets))
            heartbeat = threading.Thread(target=_heartbeat, args=(path, stop), name='monitor-heartbeat')
            heartbeat.start(); threads.append(heartbeat)
            if not no_notify:
                sender = threading.Thread(target=sender_loop, args=(path, cfg, credentials_dir, stop, fatal), name='telegram-outbox')
                try:
                    sender.start()
                except RuntimeError:
                    logger.warning('notification_worker_start_failed', extra={'fields': {'reason': 'internal_error'}})
                else:
                    threads.append(sender)
            while not stop.is_set():
                now = time.time()
                ready = [t for t in targets if once or due[t['target']] <= now]
                if ready and (once or account_due <= now):
                    shared_problem = None
                    try:
                        if now >= identity_until:
                            client.verify_identity(cfg['observer_uid'])
                            identity_until = time.time() + 3600
                    except Problem as error:
                        shared_problem = error
                    except Exception:
                        shared_problem = Problem('internal_error')
                    if shared_problem is None and account.mode == 'unknown' and account.reason in ACCOUNT_REASONS:
                        # Login health is proven by identity, not target visibility.
                        # Network-timeout episodes instead retain their counters until
                        # collection resolves the timeout or ownership transfers below.
                        recovered_at = time.time()
                        events = _advance(account, Observation(recovered_at, recovered_at), None, recovered_at, cfg)
                        db.save_account(account, events, cfg, recovered_at)
                        account_due = recovered_at
                    account_recorded = False
                    for target in ready:
                        if stop.is_set():
                            break
                        key = target['target']
                        state = states[key]
                        observation, problem = None, shared_problem
                        if problem is None:
                            wait = max(0, REQUEST_SPACING - (time.monotonic() - last_request))
                            if stop.wait(wait):
                                break
                            try:
                                observation = client.presence(key)
                            except Problem as error:
                                problem = error
                                if error.code in ACCOUNT_REASONS:
                                    shared_problem = error
                            except Exception:
                                problem = Problem('internal_error')
                            finally:
                                last_request = time.monotonic()
                        now = time.time()
                        if shared_problem and not account_recorded:
                            identity_until = 0
                            if shared_problem.code == 'network_timeout':
                                # A failed identity call is also this pass's failed target
                                # observation. Adopt the longest affected target episode
                                # BEFORE advancing once, so interface changes cannot hide
                                # the third consecutive timeout. One account owns the alert.
                                inherited = max((states[t['target']].timeout_streak
                                                 for t in ready if states[t['target']].reason == 'network_timeout'), default=0)
                                if inherited:
                                    account.mode = 'unknown'
                                    account.reason = 'network_timeout'
                                    account.unknown_since = account.unknown_since or now
                                # The target episodes are authoritative: a non-timeout
                                # result in between identity failures breaks the streak.
                                account.timeout_streak = inherited
                            events = _advance(account, None, shared_problem, now, cfg)
                            db.save_account(account, events, cfg, now)
                            account_due = now + account.next_delay
                            account_recorded = True
                        elif account.mode == 'unknown' and not shared_problem and (
                                observation is not None or (account.reason == 'network_timeout'
                                                            and problem and problem.code != 'network_timeout')):
                            # Identity succeeded and collection did not continue the
                            # timeout. Account health can recover despite an unrelated
                            # target error, without consuming that target's own episode.
                            events = _advance(account, Observation(now, now), None, now, cfg)
                            db.save_account(account, events, cfg, now)
                            account_due = now
                        covered = bool(shared_problem and account.unknown_alerted
                                       and account.notified_reason == shared_problem.code)
                        events = _advance(state, observation, problem, now, cfg, notify_allowed=not covered)
                        if covered:
                            # Only an actually queued matching shared alert supersedes a
                            # target episode. Clear its local ownership, not its counters.
                            # Local failures after identity returns can therefore alert.
                            events = [{**e, 'scope_reason': 'account'} for e in events]
                            state.unknown_alerted = False
                            state.notified_reason = ''
                            state.notified_at = 0
                        record = _sample_record(state, observation, now)
                        db.save(state, record, events, {**cfg, **target}, cfg['retention_days'],
                                announce=state.mode != 'unknown' and not no_notify)
                        due[key] = now + state.next_delay
                        _log('sample', level=logging.WARNING if state.mode == 'unknown' else logging.INFO, target=key, **record)
                        if once:
                            print(json.dumps({**record, 'target': key, 'events': [e['kind'] for e in events]}, ensure_ascii=False), flush=True)
                if once:
                    break
                next_due = min(due.values(), default=time.time() + HEARTBEAT_SECONDS)
                wait = min(HEARTBEAT_SECONDS, max(0.05, max(next_due, account_due) - time.time()))
                stop.wait(wait)
            return 1 if fatal.is_set() else 0
        finally:
            stop.set()
            # Never release the instance lock while any network/storage worker can
            # still mutate this database. Configured socket timeout bounds normal joins.
            for thread in threads:
                thread.join()
            try:
                if started:
                    db.runtime_metadata(heartbeat_at=time.time(), stopped_at=time.time(), status='stopped')
                    _log('service_stopped', pid=os.getpid(), fatal=fatal.is_set())
            finally:
                db.close()
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)


def probe(cfg, credentials_dir):
    client = DouyinClient(_session(credentials_dir), timeout=cfg.get('request_timeout', 12))
    try:
        client.verify_identity(cfg['observer_uid'])
    except Problem as error:
        return {'identity_verified': False, 'reason': error.code, 'targets': {}}
    result = {'identity_verified': True, 'targets': {}}
    for target in cfg['targets']:
        if not target['enabled']:
            continue
        try:
            observation = client.presence(target['target'])
            result['targets'][target['target']] = {**dataclasses.asdict(observation),
                'mode': 'recent' if observation.server_at - observation.active_at <= cfg['offline_after_seconds'] else 'inactive'}
        except Problem as error:
            result['targets'][target['target']] = {'mode': 'unknown', 'reason': error.code}
            if error.code in ACCOUNT_REASONS:
                break
    return result
