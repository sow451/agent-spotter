from __future__ import annotations

from pathlib import Path

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_PATH = PROJECT_ROOT / "context.md"


def _load_context_markdown() -> str:
    try:
        return CONTEXT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "# Context\n\nThe context document could not be found."


def main() -> None:
    st.set_page_config(
        page_title="agentspotter | Context",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    st.markdown(
        """
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1rem;">
          <a href="/" style="color:#111111; text-decoration:none; font-family:'Space Mono', monospace; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.08em;">
            Back
          </a>
          <a href="https://www.sowrao.com" target="_blank" rel="noreferrer" style="color:#111111; text-decoration:none; font-family:'Space Mono', monospace; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.08em;">
            Sowmya Rao
          </a>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(_load_context_markdown())


if __name__ == "__main__":
    main()
