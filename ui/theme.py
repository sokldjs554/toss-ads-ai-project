"""화면 공통 디자인 규칙 — 흰 바탕·차분한 회색·절제된 강조색 하나. 모든 카드·배지·버튼·여백이 여기 토큰을 쓴다."""
from __future__ import annotations

import html

import streamlit as st

from core.schemas import Decision, RewriteStatus, Severity
from ui.labels import DECISION_LABEL, DECISION_STYLE, SEVERITY_LABEL, SEVERITY_STYLE

CSS = """
<style>
:root { --pf-ink:#191F28; --pf-ink2:#4E5968; --pf-ink3:#6B7684; --pf-line:#E5E8EB; --pf-bg:#F7F8FA; --pf-card:#FFFFFF; --pf-accent:#2B5BD7; --pf-accent-weak:#EAF0FD; }
html, body, [data-testid="stAppViewContainer"] { background: var(--pf-bg) !important; color: var(--pf-ink); }
[data-testid="stMainBlockContainer"], [data-testid="stAppViewContainer"] .main .block-container { max-width: 1120px; padding-top: 1.2rem; padding-bottom: 4rem; }
[data-testid="stHeader"] { background: transparent; }
h1, h2, h3 { letter-spacing: -0.01em; color: var(--pf-ink); }
h1 { font-size: 1.55rem !important; font-weight: 700 !important; margin-bottom: .2rem !important; }
h2 { font-size: 1.2rem !important; font-weight: 700 !important; margin: 1.6rem 0 .5rem !important; }
h3 { font-size: 1.02rem !important; font-weight: 700 !important; margin: 1rem 0 .4rem !important; }
p, li, label, .stMarkdown { font-size: 0.95rem; line-height: 1.6; }
[data-testid="stCaptionContainer"] p { font-size: 0.82rem; color: var(--pf-ink3); }
[data-testid="stTextInput"] label p, [data-testid="stTextArea"] label p, [data-testid="stSelectbox"] label p, [data-testid="stNumberInput"] label p { font-weight: 600; color: var(--pf-ink); }
[data-testid="stExpander"] { border: 1px solid var(--pf-line); border-radius: 12px; background: var(--pf-card); }
[data-testid="stExpander"] details summary p { font-weight: 600; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px !important; border-color: var(--pf-line) !important; background: var(--pf-card); }
.stButton > button { border-radius: 10px; font-weight: 600; min-height: 42px; }
.stButton > button[kind="primary"] { background: var(--pf-accent); border-color: var(--pf-accent); }
.stButton > button:focus-visible { outline: 3px solid #9DB4F0; outline-offset: 2px; }
[data-testid="stSegmentedControl"] button, [data-testid="stRadio"] label { font-weight: 600; }
.pf-topbar { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; padding: 4px 0 10px; border-bottom: 1px solid var(--pf-line); margin-bottom: 10px; }
.pf-brand { font-weight: 800; font-size: 1.1rem; color: var(--pf-ink); }
.pf-brand small { font-weight: 500; color: var(--pf-ink3); font-size: .8rem; margin-left: 8px; }
.pf-note { font-size: .8rem; color: var(--pf-ink3); }
.pf-badge { display:inline-block; padding: 4px 10px; border-radius: 999px; font-size: .82rem; font-weight: 700; line-height: 1.3; white-space: nowrap; }
.pf-badge.big { font-size: .95rem; padding: 6px 14px; }
.pf-chip { display:inline-block; padding: 2px 8px; border-radius: 6px; font-size: .78rem; font-weight: 600; margin-right: 4px; background:#F2F4F6; color: var(--pf-ink2); }
.pf-mode { display:inline-flex; align-items:center; gap:6px; padding: 5px 10px; border-radius: 8px; background: var(--pf-accent-weak); color: #23489F; font-size: .82rem; font-weight: 700; }
.pf-mode.stored { background:#EEF5EE; color:#1E5631; }
.pf-mode.live { background:#FFF1E0; color:#8A4B00; }
.pf-ad { border:1px solid var(--pf-line); border-radius: 12px; padding: 14px 16px; background:#fff; }
.pf-ad .h { font-size: 1.05rem; font-weight: 700; line-height: 1.4; }
.pf-ad .b { font-size: .93rem; margin-top: 6px; line-height: 1.55; }
.pf-ad .c { display:inline-block; margin-top: 10px; background: var(--pf-accent); color:#fff; border-radius: 8px; padding: 4px 12px; font-size: .85rem; font-weight: 600; }
.pf-ad .d { margin-top: 10px; border-top: 1px dashed var(--pf-line); padding-top: 6px; font-size: .8rem; color: var(--pf-ink2); }
.pf-ad .t { font-size: .74rem; color: var(--pf-ink3); margin-bottom: 6px; }
mark.pf-mark { padding: 0 2px; border-radius: 3px; }
.pf-kv { font-size: .86rem; color: var(--pf-ink2); }
.pf-kv b { color: var(--pf-ink); }
.pf-reason { border-left: 3px solid var(--pf-line); padding: 6px 12px; margin: 6px 0; background:#FAFBFC; border-radius: 0 8px 8px 0; font-size: .92rem; }
.pf-reason .q { font-weight: 700; }
.pf-muted { color: var(--pf-ink3); font-size: .84rem; }
.pf-stat { border:1px solid var(--pf-line); border-radius: 12px; padding: 12px 14px; background:#fff; min-height: 86px; }
.pf-stat .k { font-size: .78rem; color: var(--pf-ink3); }
.pf-stat .v { font-size: 1.25rem; font-weight: 800; margin-top: 2px; }
.pf-stat .s { font-size: .78rem; color: var(--pf-ink2); margin-top: 2px; }
.pf-step { display:flex; gap: 10px; align-items:flex-start; padding: 6px 0; font-size: .9rem; }
.pf-step .n { width: 22px; height: 22px; border-radius: 50%; background: var(--pf-accent-weak); color:#23489F; font-size:.78rem; font-weight:700; display:flex; align-items:center; justify-content:center; flex: none; }
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"], [data-testid="stAppViewContainer"] .main .block-container { padding-left: 16px; padding-right: 16px; }
  .pf-badge.big { font-size: .88rem; }
  h1 { font-size: 1.3rem !important; }
}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def badge(decision: Decision, big: bool = False) -> str:
    fg, bg = DECISION_STYLE[decision]
    return f'<span class="pf-badge{" big" if big else ""}" style="color:{fg};background:{bg}" role="status">{html.escape(DECISION_LABEL[decision])}</span>'


def chip(text: str) -> str:
    return f'<span class="pf-chip">{html.escape(text)}</span>'


def severity_chip(sev: Severity) -> str:
    return f'<span class="pf-chip" style="color:{SEVERITY_STYLE[sev]}">{html.escape(SEVERITY_LABEL[sev])}</span>'


def mode_pill(kind: str, text: str) -> str:
    cls = {"stored": " stored", "live": " live", "partial": " live"}.get(kind, "")
    return f'<span class="pf-mode{cls}">{html.escape(text)}</span>'


def stat(label: str, value: str, sub: str = "") -> str:
    return f'<div class="pf-stat"><div class="k">{html.escape(label)}</div><div class="v">{html.escape(value)}</div><div class="s">{html.escape(sub)}</div></div>'


def rewrite_status_label(s: RewriteStatus) -> str:
    from ui.labels import REWRITE_STATUS_LABEL

    return REWRITE_STATUS_LABEL[s]
