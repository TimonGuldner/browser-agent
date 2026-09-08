import os

from browser_use import Agent, ChatAnthropic, ChatBrowserUse, ChatGoogle, ChatOpenAI

from agent.config import settings


def build_llm():
    provider = settings.llm_provider.lower()
    if provider == 'browser-use':
        return ChatBrowserUse(model=settings.llm_model)
    if provider == 'google':
        return ChatGoogle(model=settings.llm_model)
    if provider == 'anthropic':
        return ChatAnthropic(model=settings.llm_model)
    return ChatOpenAI(model=settings.llm_model)


async def run_browser_task(task: str) -> dict:
    # Provider SDKs read their keys from the environment. Keep secrets out of source control.
    if settings.openai_api_key:
        os.environ['OPENAI_API_KEY'] = settings.openai_api_key
    if settings.browser_use_api_key:
        os.environ['BROWSER_USE_API_KEY'] = settings.browser_use_api_key
    if settings.google_api_key:
        os.environ['GOOGLE_API_KEY'] = settings.google_api_key
    if settings.anthropic_api_key:
        os.environ['ANTHROPIC_API_KEY'] = settings.anthropic_api_key

    agent = Agent(task=task, llm=build_llm())
    history = await agent.run(max_steps=settings.max_steps)
    return {
        'final_result': history.final_result(),
        'is_done': history.is_done(),
        'is_successful': history.is_successful(),
        'errors': [str(error) for error in history.errors() if error],
    }
