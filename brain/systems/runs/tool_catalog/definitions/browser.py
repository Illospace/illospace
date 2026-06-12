"""Browser session tool schemas."""

from __future__ import annotations


BROWSER_SESSION_OPEN_TOOL = {
    "name": "browser_session_open",
    "description": (
        "Create or reuse a live server-side browser session for the current Cortex thought. "
        "Use this when a task requires real browsing, JavaScript execution, login flows, "
        "or a live browser viewport in the thought."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Optional initial URL to open."},
            "viewport_width": {"type": "integer", "default": 1280},
            "viewport_height": {"type": "integer", "default": 800},
            "storage_mode": {
                "type": "string",
                "enum": ["ephemeral", "idea"],
                "default": "ephemeral",
                "description": "Whether login/session state persists for the current thought.",
            },
            "allow_downloads": {
                "type": "boolean",
                "default": False,
                "description": "Allow file downloads into the thought workspace uploads area.",
            },
            "allow_file_uploads": {
                "type": "boolean",
                "default": True,
                "description": "Allow uploading existing Cortex attachments into file inputs.",
            },
        },
    },
}

BROWSER_NAVIGATE_TOOL = {
    "name": "browser_navigate",
    "description": "Navigate the active thought browser session to a URL.",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to open"},
        },
        "required": ["url"],
    },
}

BROWSER_CLICK_TOOL = {
    "name": "browser_click",
    "description": "Click in the active browser session by selector or viewport coordinates.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Optional CSS selector target"},
            "x": {"type": "number", "description": "Viewport X coordinate"},
            "y": {"type": "number", "description": "Viewport Y coordinate"},
        },
    },
}

BROWSER_TYPE_TOOL = {
    "name": "browser_type",
    "description": "Type text into the active browser session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type"},
            "selector": {"type": "string", "description": "Optional CSS selector to focus first"},
            "press_enter": {"type": "boolean", "default": False},
        },
        "required": ["text"],
    },
}

BROWSER_KEY_TOOL = {
    "name": "browser_key",
    "description": "Press a keyboard key in the active browser session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Keyboard key, e.g. Enter or Escape"},
        },
        "required": ["key"],
    },
}

BROWSER_BACK_TOOL = {
    "name": "browser_back",
    "description": "Navigate backward in the active browser session history.",
    "input_schema": {"type": "object", "properties": {}},
}

BROWSER_FORWARD_TOOL = {
    "name": "browser_forward",
    "description": "Navigate forward in the active browser session history.",
    "input_schema": {"type": "object", "properties": {}},
}

BROWSER_NEW_TAB_TOOL = {
    "name": "browser_new_tab",
    "description": "Open a new tab in the active browser session, optionally navigating it immediately.",
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Optional initial URL"},
        },
    },
}

BROWSER_SWITCH_TAB_TOOL = {
    "name": "browser_switch_tab",
    "description": "Switch to a tab by index in the active browser session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Tab index"},
        },
        "required": ["index"],
    },
}

BROWSER_CLOSE_TAB_TOOL = {
    "name": "browser_close_tab",
    "description": "Close a tab by index, or the current tab if omitted.",
    "input_schema": {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "Optional tab index"},
        },
    },
}

BROWSER_LIST_TABS_TOOL = {
    "name": "browser_list_tabs",
    "description": "List tabs in the active browser session.",
    "input_schema": {"type": "object", "properties": {}},
}

BROWSER_WAIT_TOOL = {
    "name": "browser_wait",
    "description": "Wait for the active browser session to reach a page state or selector.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Optional CSS selector to wait for"},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle"],
                "default": "load",
            },
            "timeout_ms": {"type": "integer", "default": 10000},
        },
    },
}

BROWSER_EXTRACT_TOOL = {
    "name": "browser_extract",
    "description": "Extract text or HTML from the active browser session, optionally scoped to a selector.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Optional CSS selector target"},
            "mode": {
                "type": "string",
                "enum": ["text", "html", "markdown"],
                "default": "text",
            },
            "max_chars": {"type": "integer", "default": 6000},
        },
    },
}

BROWSER_DISCOVER_TOOL = {
    "name": "browser_discover",
    "description": "List likely interactive elements on the page with suggested selectors and bounds.",
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Optional selector used to scope discovery",
                "default": "a,button,input,textarea,select,[role='button']",
            },
            "max_results": {"type": "integer", "default": 40},
        },
    },
}

BROWSER_UPLOAD_ATTACHMENT_TOOL = {
    "name": "browser_upload_attachment",
    "description": (
        "Upload an existing Cortex attachment into a file input inside the active browser session. "
        "attachment_url must be a Cortex /static/uploads/... URL."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector for the file input"},
            "attachment_url": {"type": "string", "description": "Cortex attachment URL under /static/uploads/"},
        },
        "required": ["selector", "attachment_url"],
    },
}

BROWSER_SNAPSHOT_TOOL = {
    "name": "browser_snapshot",
    "description": "Capture the current browser viewport, return it as a model-visible screenshot, and optionally persist it into the thought.",
    "input_schema": {
        "type": "object",
        "properties": {
            "persist": {"type": "boolean", "default": False},
            "title": {"type": "string", "description": "Optional snapshot title"},
        },
    },
}

BROWSER_SAVE_SCREENSHOT_TOOL = {
    "name": "browser_save_screenshot",
    "description": "Save a PNG screenshot of the current page into the thought workspace uploads area. Use the returned download_url when giving the user a link.",
    "input_schema": {
        "type": "object",
        "properties": {
            "full_page": {"type": "boolean", "default": True},
        },
    },
}

BROWSER_PRINT_PDF_TOOL = {
    "name": "browser_print_pdf",
    "description": "Export the current page as a PDF into the thought workspace uploads area. Use the returned download_url when giving the user a link.",
    "input_schema": {
        "type": "object",
        "properties": {
            "landscape": {"type": "boolean", "default": False},
        },
    },
}

BROWSER_CLOSE_TOOL = {
    "name": "browser_close",
    "description": "Close the active thought browser session.",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

BROWSER_TOOL = {
    "name": "browser",
    "description": (
        "Namespace tool for controlling or inspecting the live browser session attached to the current Cortex thought. "
        "Set action='help' to see sub-actions and their required arguments. Use action='open' before navigation or interaction. "
        "Actions that change browser state return a model-visible screenshot; use action='observe' to refresh that screenshot explicitly, "
        "and action='discover' or action='extract' when DOM/text detail is needed. "
        "For tasks that may download a file, open the session with allow_downloads=true."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "help",
                    "open",
                    "navigate",
                    "click",
                    "type",
                    "key",
                    "back",
                    "forward",
                    "new_tab",
                    "switch_tab",
                    "close_tab",
                    "list_tabs",
                    "wait",
                    "observe",
                    "extract",
                    "discover",
                    "upload_attachment",
                    "snapshot",
                    "save_screenshot",
                    "print_pdf",
                    "close",
                ],
                "description": "Browser sub-action to run.",
            },
            "operation": {
                "type": "string",
                "description": "Optional sub-action name to inspect when action is help.",
            },
            "url": {"type": "string", "description": "URL for open, navigate, or new_tab."},
            "viewport_width": {"type": "integer", "default": 1280},
            "viewport_height": {"type": "integer", "default": 800},
            "storage_mode": {
                "type": "string",
                "enum": ["ephemeral", "idea"],
                "default": "ephemeral",
                "description": "Whether login/session state persists for the current thought.",
            },
            "allow_downloads": {"type": "boolean", "default": False},
            "allow_file_uploads": {"type": "boolean", "default": True},
            "selector": {"type": "string", "description": "CSS selector for click/type/wait/extract/discover/upload."},
            "x": {"type": "number", "description": "Viewport X coordinate for click."},
            "y": {"type": "number", "description": "Viewport Y coordinate for click."},
            "text": {"type": "string", "description": "Text to type."},
            "press_enter": {"type": "boolean", "default": False},
            "key": {"type": "string", "description": "Keyboard key, e.g. Enter or Escape."},
            "index": {"type": "integer", "description": "Tab index for switch_tab or close_tab."},
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle"],
                "default": "load",
            },
            "timeout_ms": {"type": "integer", "default": 10000},
            "mode": {
                "type": "string",
                "enum": ["text", "html", "markdown"],
                "default": "text",
                "description": "Extraction mode for extract.",
            },
            "max_chars": {"type": "integer", "default": 6000},
            "max_results": {"type": "integer", "default": 40},
            "attachment_url": {"type": "string", "description": "Cortex /static/uploads/... attachment URL for upload_attachment."},
            "persist": {"type": "boolean", "default": False},
            "title": {"type": "string", "description": "Optional snapshot title."},
            "full_page": {"type": "boolean", "default": True},
            "landscape": {"type": "boolean", "default": False},
        },
        "required": ["action"],
    },
}


__all__ = [
    "BROWSER_BACK_TOOL",
    "BROWSER_CLICK_TOOL",
    "BROWSER_CLOSE_TAB_TOOL",
    "BROWSER_CLOSE_TOOL",
    "BROWSER_DISCOVER_TOOL",
    "BROWSER_EXTRACT_TOOL",
    "BROWSER_FORWARD_TOOL",
    "BROWSER_KEY_TOOL",
    "BROWSER_LIST_TABS_TOOL",
    "BROWSER_NAVIGATE_TOOL",
    "BROWSER_NEW_TAB_TOOL",
    "BROWSER_PRINT_PDF_TOOL",
    "BROWSER_SAVE_SCREENSHOT_TOOL",
    "BROWSER_SESSION_OPEN_TOOL",
    "BROWSER_SNAPSHOT_TOOL",
    "BROWSER_SWITCH_TAB_TOOL",
    "BROWSER_TOOL",
    "BROWSER_TYPE_TOOL",
    "BROWSER_UPLOAD_ATTACHMENT_TOOL",
    "BROWSER_WAIT_TOOL",
]
