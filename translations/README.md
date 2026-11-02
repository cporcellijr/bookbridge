# UI translations

The web UI is translated with Flask-Babel. Each language lives in
`translations/<lang>/LC_MESSAGES/messages.po`. Only the `.po` source is in git:
the Docker build compiles it to `messages.mo`, and a language without a compiled
catalog is not offered in the language picker. Untranslated strings fall back to
English.

What gets translated: user-facing text in the templates, including the
strings their scripts and `static/js` show (see `templates/_i18n.html`), and
the integration field labels in `src/utils/user_config.py`. Log messages,
messages the server sends back as JSON errors, setting keys, API/JSON field
names and internal identifiers stay English.

Keep placeholders exactly as in the English text: `%(name)s` (filled by
Jinja) and `{name}` (filled in JavaScript). Entries with a context
(`msgctxt "book state"`) are separate from the same word without one.

## Update a catalog after template changes

```bash
pybabel extract -F babel.cfg -o messages.pot .
pybabel update -i messages.pot -d translations --no-wrap
```

`update` adds new strings with an empty translation and marks changed ones
`#, fuzzy`. Fuzzy and empty entries are skipped at compile time, so the page
shows the English text until someone translates them; remove the `fuzzy` flag
once the translation is checked.

## Run with translations outside Docker

```bash
pybabel compile -d translations
```

## Add a language

```bash
pybabel init -i messages.pot -d translations -l <lang>
```
