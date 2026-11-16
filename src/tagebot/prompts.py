"""Prompt and message templates.

Attributes:
    PARSE_SYSTEM_PROMPT: System prompt for extracting numeric ratings from a
        message. Placeholder: ``{parameters}`` (comma-separated names).
    CHAT_SYSTEM_PROMPT: Default system prompt for free-form chat.
    RATING_REMINDER: Check-in message requesting ratings. Placeholders:
        ``{parameters}`` (bulleted list), ``{example}`` (bare numbers) and
        ``{example_named}`` (name/value pairs). Its first line is used to
        recognise replies to this message.
    DIARY_REMINDER: Check-in message requesting a diary entry. Its first line
        is used to recognise replies to this message.
"""


PARSE_SYSTEM_PROMPT = """\
You extract daily self-ratings from a short message. The message may be German or English
and may be a speech transcript (numbers can be written as words, e.g. "sieben" or "seven").

The parameters are: {parameters}

Rules:
- Each rating is an integer from 1 to 10.
- If the message lists bare numbers without names, assign them to the parameters in the order given above.
- Map synonyms and translations to the parameter names, e.g. "Stimmung" -> mood, "Schmerz" -> pain.
- If a parameter is not mentioned, use null. Never guess.
- Return only the JSON object that matches the schema.
"""

CHAT_SYSTEM_PROMPT = """\
You are a friendly personal assistant chatting via Signal.
Answer in the language the user writes in (German or English). Keep answers short, they are read on a phone.
"""

RATING_REMINDER = """\
🌙 Evening check-in. Please reply to this message and rate from 1 to 10:
{parameters}

e.g. "{example}" (in this order) or "{example_named}". Voice messages work too."""

DIARY_REMINDER = """\
📝 What happened today and how did you feel about it?
Reply to this message with text or voice messages, as many as you like."""
