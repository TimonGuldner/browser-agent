from browser_use import Agent

from agent.config import settings
from agent import llm_router


def build_llm():
    slots = llm_router.configured_slots(llm_router.TASK_BROWSER_REASONING)
    if not slots:
        raise RuntimeError("BLOCKED_LLM_PROVIDER: no configured provider for browser task")
    return llm_router.create_browser_llm(slots[0])


async def run_browser_task(task: str) -> dict:
    slots = llm_router.configured_slots(llm_router.TASK_BROWSER_REASONING)
    if not slots:
        raise RuntimeError("BLOCKED_LLM_PROVIDER: no configured provider for browser task")
    attempts = []
    last_error = None
    for slot in slots:
        try:
            llm_router.activate_slot(slot)
            agent = Agent(task=task, llm=llm_router.create_browser_llm(slot))
            history = await agent.run(max_steps=settings.max_steps)
            errors = [str(error) for error in history.errors() if error]
            result = {
                'final_result': history.final_result(),
                'is_done': history.is_done(),
                'is_successful': history.is_successful(),
                'errors': errors,
                'llm_task_type': llm_router.TASK_BROWSER_REASONING,
                'llm_provider': slot.provider,
                'llm_model': slot.model,
                'llm_key_slot': slot.key_slot,
                'provider_attempts': attempts + [{'provider': slot.provider, 'key_slot': slot.key_slot, 'status': 'completed' if history.is_successful() else 'failed'}],
                'provider_failover_used': bool(attempts),
            }
            if history.is_successful():
                return result
            error_text = ' '.join(errors)
            if not llm_router.should_failover(error_text):
                return result
            attempts.append({'provider': slot.provider, 'key_slot': slot.key_slot, 'status': 'failed', 'error_class': llm_router.provider_health(error_text)})
        except Exception as exc:
            last_error = exc
            text = str(exc)
            attempts.append({'provider': slot.provider, 'key_slot': slot.key_slot, 'status': 'failed', 'error_class': llm_router.provider_health(text)})
            if not llm_router.should_failover(text):
                raise
    raise RuntimeError(f"All routed browser LLM providers failed: {str(last_error)[:1000]}")
