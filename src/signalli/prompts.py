





RECIPIENT_SCHEMA = {
    "type": "object",
    "properties": {
        "RECV_NAME1":       {"type": "string", "description": "Person or company name, first line"},
        "RECV_NAME2":       {"type": "string", "description": "Extra address line such as c/o, floor, or department. Empty if none"},
        "RECV_STREET":      {"type": "string", "description": "Street name without house number"},
        "RECV_HOUSENUMBER": {"type": "string", "description": "House number, may include a letter like 12a"},
        "RECV_PLZ":         {"type": "string", "description": "Postal code, digits only"},
        "RECV_CITY":        {"type": "string", "description": "City name"},
        "RECV_COUNTRY":     {"type": "string", "description": "Country name. Empty if unknown"},
        "RECV_EMAIL_OR_EMPTY": {"type": "string", "description": "Ignore, always empty"},
    },
    "required": ["RECV_NAME1", "RECV_STREET", "RECV_HOUSENUMBER", "RECV_PLZ", "RECV_CITY"],
}

SYSTEM_PROMPT = """\
You extract postal recipient data from pasted text and map it to JSON fields.
Addresses are mostly German, but some are British or from other countries.

The input usually has up to four lines, one item per line:
1. Name                         -> RECV_NAME1 (person or company name)
2. Street line                  -> RECV_STREET and RECV_HOUSENUMBER
3. Postcode and city            -> RECV_PLZ and RECV_CITY
4. Country                      -> RECV_COUNTRY

Street line formats:
- German: "Straße Hausnummer", e.g. "Hauptstraße 12a". Street first, number last.
- British: "Hausnummer Straße", e.g. "12 Baker Street" or "92 Hillfall". The number comes first.
  Put the house number in RECV_HOUSENUMBER and the street name in RECV_STREET.
  Flat, unit, or floor text goes into RECV_NAME2.

Postcode and city:
- German: "12345 Ort". PLZ is five digits, city is the rest.
- British: "Ort SW1A 1AA" or "SW1A 1AA Ort". The postcode is the trailing or leading alphanumeric group, and it keeps its space.
- Do not include a country prefix such as "D-" or "GB-" in RECV_PLZ.

Other rules:
- Keep house numbers as written, including letters and ranges such as "12a", "5-7", "3/4".
- If a line has extra information such as "c/o ..." or "Etage 2", put it in RECV_NAME2.
- RECV_COUNTRY is the country name as written, e.g. "Deutschland", "Vereinigtes Königreich", "England". Leave it empty if the country line is missing. Do not guess.
- Never invent or correct data. If a value is absent, return an empty string.
- Ignore phone numbers, emails, and other text that does not fit these fields.
- Return only the JSON object that matches the schema.
"""

FEW_SHOT = [
    ("Max Mustermann\nHauptstraße 12a\n10115 Berlin\nDeutschland",
     {"RECV_NAME1": "Max Mustermann", "RECV_STREET": "Hauptstraße",
      "RECV_HOUSENUMBER": "12a", "RECV_PLZ": "10115",
      "RECV_CITY": "Berlin", "RECV_COUNTRY": "Deutschland"}),
    ("Jane Smith\n12 Baker Street\nLondon SW1A 1AA\nEngland",
     {"RECV_NAME1": "Jane Smith", "RECV_STREET": "Baker Street",
      "RECV_HOUSENUMBER": "12", "RECV_PLZ": "SW1A 1AA",
      "RECV_CITY": "London", "RECV_COUNTRY": "England"}),
]

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
