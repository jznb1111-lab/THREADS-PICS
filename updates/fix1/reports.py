"""Отчёты: цифры поста через 24 ч, сводка по понедельникам, переходы в воронку @WelcomeSyndBot по меткам a/b/c."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TAG_COLS = ("account", "start_param", "start", "source", "src", "tag", "ref", "utm", "label", "payload", "origin", "deep_link")
TIME_COLS = ("created_at", "first_seen", "joined_at", "started_at", "ts", "date", "timestamp", "created")
TAG_TO_ACC = {"a": "A", "b": "B", "c": "C"}


def _tz(cfg):
    return ZoneInfo(cfg.get("timezone", "Europe/Chisinau"))


def _latest_metrics(db, post_id):
    return db.execute("SELECT * FROM metrics WHERE post_id=? ORDER BY taken_at DESC LIMIT 1", (post_id,)).fetchone()


def _handle(cfg, acc):
    u = ((cfg.get("accounts") or {}).get(acc) or {}).get("username")
    return f"@{u.lstrip('@')}" if u else acc


def _first_line(text, n=60):
    line = (text or "").strip().split("\n")[0]
    return line if len(line) <= n else line[:n - 1] + "…"


def fmt_metrics(m):
    if not m:
        return "нет данных"
    return f"👁 {m['views'] or 0}  ❤️ {m['likes'] or 0}  💬 {m['replies'] or 0}  🔁 {(m['reposts'] or 0) + (m['quotes'] or 0)}"


# ---------- через 24 часа ----------
def due_24h(db):
    """Посты, которым исполнилось 24 ч и о которых ещё не отчитывались."""
    return list(db.execute(
        """SELECT p.*, d.text FROM posts p LEFT JOIN drafts d ON d.id=p.draft_id
           WHERE p.published_at <= datetime('now','-24 hours') AND p.published_at >= datetime('now','-4 days')
             AND p.id NOT IN (SELECT post_id FROM reported WHERE kind='24h') ORDER BY p.published_at"""))


def report_24h(cfg, db) -> str | None:
    from .cli import client_for
    rows = due_24h(db)
    if not rows:
        return None
    lines = ["📊 Сутки после публикации:"]
    for p in rows:
        try:
            m = client_for(cfg, p["account"]).insights(p["media_id"])
            db.execute("INSERT INTO metrics(post_id,views,likes,replies,reposts,quotes,shares) VALUES(?,?,?,?,?,?,?)",
                       (p["id"], m.get("views"), m.get("likes"), m.get("replies"), m.get("reposts"), m.get("quotes"), m.get("shares")))
        except Exception as e:  # noqa: BLE001
            if "5258005" in str(e):
                raise
            print(f"24ч пост {p['id']}: {str(e)[:150]}")
        db.execute("INSERT OR IGNORE INTO reported(post_id,kind) VALUES(?, '24h')", (p["id"],))
        lines.append(f"\n#{p['draft_id']} · {_handle(cfg, p['account'])} · {_first_line(p['text'])}\n"
                     f"{fmt_metrics(_latest_metrics(db, p['id']))}\n{p['permalink'] or ''}")
    db.commit()
    return "\n".join(lines)


# ---------- переходы в воронку ----------
def _find_funnel_db(cfg):
    p = cfg.get("funnel_db")
    if p:
        p = Path(p)
        return p if p.is_absolute() else (ROOT / p)
    for base in (ROOT.parent / "tg-funnel", ROOT.parent / "funnel", ROOT.parent):
        if base.is_dir():
            for pat in ("*.sqlite3", "*.sqlite", "*.db"):
                found = sorted(base.glob(pat)) + sorted(base.glob(f"*/{pat}"))
                if found:
                    return found[0]
    return None


def _parse_time(v):
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            x = float(v)
            return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, tz=timezone.utc)
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T", 1))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, OSError):
        return None


def _norm_tag(v):
    s = str(v or "").strip().lower()
    for pre in ("/start ", "start=", "?start=", "src_", "from_"):
        if s.startswith(pre):
            s = s[len(pre):]
    s = s.split("_")[0]  # метки вида a_1121_3 → a
    return s if s in TAG_TO_ACC else None


def funnel_starts(cfg, since: datetime, until: datetime | None = None) -> dict | None:
    """Сколько новых людей пришло в бота-воронку по меткам a/b/c за период. None, если базу не нашли.
    Базу воронки только читаем, ничего в ней не меняем."""
    path = _find_funnel_db(cfg)
    if not path or not path.exists():
        return None
    until = until or datetime.now(timezone.utc)
    counts = {"A": 0, "B": 0, "C": 0}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        best = None
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            cols = [r[1].lower() for r in con.execute(f'PRAGMA table_info("{t}")')]
            tag = next((c for c in TAG_COLS if c in cols), None)
            tm = next((c for c in TIME_COLS if c in cols), None)
            if tag and tm and (best is None or t.lower() in ("users", "leads")):
                best = (t, tag, tm)
        if not best:
            return None
        t, tag, tm = best
        for r in con.execute(f'SELECT "{tag}" g, "{tm}" w FROM "{t}"'):
            acc, when = TAG_TO_ACC.get(_norm_tag(r["g"]) or ""), _parse_time(r["w"])
            if acc and when and since <= when < until:
                counts[acc] += 1
        con.close()
    except sqlite3.Error as e:
        print(f"воронка: {e}")
        return None
    return counts


# ---------- сводка за неделю ----------
def _spec(d):
    try:
        return json.loads(d or "{}")
    except ValueError:
        return {}


def week_rows(db, days=7):
    q = """SELECT p.id post_id, p.account, p.permalink, p.published_at, d.id draft_id, d.text, d.visual_kind, d.visual_spec,
                  m.views, m.likes, m.replies, m.reposts, m.quotes
           FROM posts p JOIN drafts d ON d.id=p.draft_id
           LEFT JOIN metrics m ON m.rowid=(SELECT rowid FROM metrics WHERE post_id=p.id ORDER BY taken_at DESC LIMIT 1)
           WHERE p.published_at >= datetime('now', ?) ORDER BY p.published_at"""
    out = []
    for r in db.execute(q, (f"-{days} days",)):
        r = dict(r)
        s = _spec(r.pop("visual_spec"))
        r["role"], r["hook"] = s.get("role") or "—", s.get("hook") or "—"
        out.append(r)
    return out


def group(rows, key):
    g = {}
    for r in rows:
        k = r[key] or "—"
        x = g.setdefault(k, {"n": 0, "views": 0, "likes": 0, "replies": 0})
        x["n"] += 1
        for f in ("views", "likes", "replies"):
            x[f] += r[f] or 0
    return {k: {"n": v["n"], **{f: round(v[f] / v["n"]) for f in ("views", "likes", "replies")}} for k, v in g.items()}


def weekly_summary(cfg, db) -> str:
    rows = week_rows(db, 7)
    now = datetime.now(timezone.utc)
    lines = ["📈 Сводка за неделю"]
    if not rows:
        lines.append("Постов за неделю не было.")
    else:
        lines.append(f"Постов: {len(rows)}, просмотров всего: {sum(r['views'] or 0 for r in rows)}")
        for title, key in (("По аккаунтам", "account"), ("По задаче поста", "role"), ("По хуку", "hook")):
            lines.append(f"\n{title} (в среднем на пост):")
            for k, v in sorted(group(rows, key).items(), key=lambda kv: -kv[1]["views"]):
                name = _handle(cfg, k) if key == "account" else k
                lines.append(f"  {name}: {v['n']} пост. · 👁 {v['views']} ❤️ {v['likes']} 💬 {v['replies']}")
        top = sorted(rows, key=lambda r: -(r["views"] or 0))[:3]
        lines.append("\nЛучшие:")
        for r in top:
            lines.append(f"  #{r['draft_id']} {_handle(cfg, r['account'])} 👁 {r['views'] or 0}: {_first_line(r['text'], 50)}")
    f = funnel_starts(cfg, now - timedelta(days=7), now)
    if f is None:
        lines.append("\nПереходы в бота-воронку: не нашёл её базу (укажите funnel_db в config.json).")
    else:
        prev = funnel_starts(cfg, now - timedelta(days=14), now - timedelta(days=7)) or {}
        lines.append("\nПереходы в бота-воронку (новые люди):")
        for acc in ("A", "B", "C"):
            lines.append(f"  {_handle(cfg, acc)} (?start={acc.lower()}): {f[acc]}  (неделей раньше {prev.get(acc, 0)})")
    return "\n".join(lines)


def is_monday_due(cfg, db) -> bool:
    from .tg import kv_get
    now = datetime.now(_tz(cfg))
    week = now.strftime("%G-%V")
    return now.weekday() == 0 and now.hour >= 10 and kv_get(db, "weekly_sent") != week


def mark_monday(cfg, db):
    from .tg import kv_set
    kv_set(db, "weekly_sent", datetime.now(_tz(cfg)).strftime("%G-%V"))
    db.commit()
