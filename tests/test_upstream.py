from arcade_tool_selector.proxy.upstream import gateway_purpose

# What an Arcade gateway sends in initialize (shape captured 2026-10-12): its block, then the gateway's instructions.
ARCADE_INSTRUCTIONS = (
    "<system>\nBefore starting a job whose tools need the end user's authorization, ...\n</system>\n\n\n"
    "## Available Apps\n\nThe calendar apps:\n<available_apps>\n- GoogleCalendar — connected as me@acme.com\n</available_apps>\n\n"
    "Meeting scheduler. Reads the user's calendar and creates events.\n"
)


def test_gateway_purpose_is_the_text_after_arcades_block():
    assert gateway_purpose(ARCADE_INSTRUCTIONS) == "Meeting scheduler. Reads the user's calendar and creates events."


def test_no_purpose_set_on_the_gateway():
    assert gateway_purpose("<system>...</system>\n<available_apps>\n- Gmail\n</available_apps>\n") == ""
