import pdfplumber
import uuid
from agent.Supervisor.Supervisor import Supervisor
from src.api.model_store import get_selected_model
from src.api.session_store import session_store


def start_interview(pdf_file, job_description: str, num_questions: int = 5) -> dict:
    # extract text from PDF
    with pdfplumber.open(pdf_file) as pdf:
        resume = "\n".join(page.extract_text() for page in pdf.pages)

    # build the graph
    supervisor = Supervisor(llm=get_selected_model())
    graph = supervisor.build()
    session_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": session_id}}

    # run until first interrupt (planner + first question)
    graph.invoke(
        {
            "raw_resume": resume,
            "raw_job_description": job_description,
            "requested_question_count": num_questions,
        },
        config=config,
    )

    # get the question from the interrupt
    state = graph.get_state(config)
    first_question = state.tasks[0].interrupts[0].value

    # store graph + config for this session
    session_store[session_id] = {"graph": graph, "config": config}

    return {"session_id": session_id, "first_question": first_question}
