"""
Regression tests for logic_automation.py's JXA string-building --
verifies a value containing a double-quote (the character that would
otherwise let it break out of a JXA string literal and inject arbitrary
script) gets escaped rather than passed through raw.

Not proof of a previously-exploited bug (every real call site always
passed fixed literals), but this locks in the hardening explicitly
requested: no unescaped interpolation into a generated script, ever,
regardless of whether today's callers happen to be safe.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services import logic_automation


def test_jxa_string_literal_escapes_quotes_and_backslashes():
    assert logic_automation._jxa_string_literal('Logic Pro"); doStuff(); //') == 'Logic Pro\\"); doStuff(); //'
    assert logic_automation._jxa_string_literal("back\\slash") == "back\\\\slash"


def test_activate_app_escapes_a_malicious_app_name():
    malicious = 'Logic Pro"); Application("Terminal").doShellScript("echo pwned'

    with patch.object(logic_automation, "run_jxa") as mock_run_jxa:
        logic_automation.activate_app(malicious)

    script = mock_run_jxa.call_args.args[0]
    # The malicious quote must appear ESCAPED (\") in the generated
    # script, never as a raw unescaped quote that would close the
    # JXA string literal early.
    assert '\\"' in script
    assert 'Application("Terminal").doShellScript' not in script.replace('\\"', "")


def test_press_button_escapes_a_malicious_button_fragment():
    malicious = 'x"); Application("Terminal").doShellScript("echo pwned'

    with patch.object(logic_automation, "run_jxa") as mock_run_jxa:
        logic_automation.press_button("Logic Pro", malicious)

    script = mock_run_jxa.call_args.args[0]
    assert '\\"' in script


def test_simulate_keystroke_escapes_a_malicious_key():
    malicious = 'a"); Application("Terminal").doShellScript("echo pwned'

    with patch.object(logic_automation, "run_jxa") as mock_run_jxa:
        logic_automation.simulate_keystroke(malicious)

    script = mock_run_jxa.call_args.args[0]
    assert '\\"' in script
