from agent.config.llm import DEFAULT_MODEL, MODELS

_state = {"selected_model": DEFAULT_MODEL}


def get_selected_model_name() -> str:
    return _state["selected_model"]


def set_selected_model_name(name: str) -> None:
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}, must be one of {sorted(MODELS)}")
    _state["selected_model"] = name


def get_selected_model():
    return MODELS[get_selected_model_name()]
