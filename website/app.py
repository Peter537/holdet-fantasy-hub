"""Streamlit entrypoint and native page router for Holdet Fantasy Hub."""

from __future__ import annotations

from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from website.navigation import create_pages, redirect_legacy_query, selected_page_id
from website.ui import (
    build_ui_context,
    install_styles,
    render_shared_shell,
    render_sidebar,
    set_ui_context,
)


st.set_page_config(
    page_title="Holdet Fantasy Hub",
    page_icon=":material/trophy:",
    layout="wide",
    initial_sidebar_state="auto",
)
install_styles()

pages = create_pages()
selected_page = st.navigation(list(pages.values()), position="hidden")
context = build_ui_context()
set_ui_context(context)
page_id = selected_page_id(selected_page, pages)

# Register pages before redirecting so st.switch_page recognizes every target.
redirect_legacy_query(page_id)

render_sidebar(context, page_id)
render_shared_shell(context, page_id)
selected_page.run()
