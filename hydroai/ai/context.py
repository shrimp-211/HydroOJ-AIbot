"""Build bounded repair history without cutting the problem or current code."""
from copy import deepcopy


def repair_messages(history, code, ext, feedback):
    initial = []
    for role in ('system', 'user'):
        message = next((m for m in history if m.get('role') == role), None)
        if message is not None:
            initial.append(deepcopy(message))
    old_feedback = [m['content'] for m in history[2:]
                    if m.get('role') == 'user' and isinstance(m.get('content'), str)]
    notes = '\n'.join(text[:300] for text in old_feedback[-2:])
    if notes:
        feedback = '## 最近失败摘要（避免重复）\n' + notes + '\n\n' + feedback
    return initial + [
        {'role': 'assistant', 'content': f'当前待修正的代码：\n```{ext}\n{code}\n```'},
        {'role': 'user', 'content': feedback},
    ]
