#!/usr/bin/env python3
"""Apply a small, fail-closed chat-search patch to Open WebUI 0.13.3."""
from pathlib import Path
import py_compile

root = Path('/app/backend/open_webui')
models = root / 'models/chats.py'
builtin = root / 'tools/builtin.py'

def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise SystemExit(f'Upstream changed: expected exactly one match for {old[:100]!r}')
    return source.replace(old, new, 1)

text = models.read_text()
text = replace_once(text, "re.findall(r'[a-z0-9]+', text.lower())", "re.findall(r'[^\\W_]+', text.lower())")
models.write_text(text)

text = builtin.read_text()
old = '''        snippets = await Chats.get_chat_search_snippets([chat.id for chat in matched], user_id, query)
'''
new = '''        # Strict phrase/all-terms search can fail on conversational queries
        # (inflected or misspelled words). Fall back to per-term OR matching
        # only when the strict pass returns no eligible chats, and leave the
        # normal UI search path untouched.
        content_query = chat_search_content_query(query)
        if not matched and content_query == query.lower().strip():
            # Terms under 3 chars are too noisy to OR on; a query made only of
            # them falls through unchanged (empty result, same as before).
            terms = [term for term in chat_search_terms(content_query) if len(term) >= 3][:5]
            if terms:
                limit = min(max(count * 3, 15), 100)
                partials = await asyncio.gather(
                    *(
                        Chats.get_chats_by_user_id_and_search_text(
                            user_id=user_id,
                            search_text=term,
                            include_archived=False,
                            skip=0,
                            limit=limit,
                        )
                        for term in terms
                    )
                )
                candidates = {}
                scores = {}
                for partial in partials:
                    for chat in partial:
                        if __chat_id__ and chat.id == __chat_id__:
                            continue
                        if start_timestamp and chat.updated_at < start_timestamp:
                            continue
                        if end_timestamp and chat.updated_at > end_timestamp:
                            continue
                        candidates[chat.id] = chat
                        scores[chat.id] = scores.get(chat.id, 0) + 1
                matched = sorted(
                    candidates.values(), key=lambda c: (-scores[c.id], -c.updated_at, c.id)
                )[:count]

        snippets = await Chats.get_chat_search_snippets([chat.id for chat in matched], user_id, query)
'''
text = replace_once(text, old, new)
builtin.write_text(text)
for path in (models, builtin):
    py_compile.compile(str(path), doraise=True)
print('Chat search patch applied and compiled')
