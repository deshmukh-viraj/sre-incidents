import os
from pathlib import Path
from dotenv import load_dotenv

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from src.graph.state import AgentState
from src.agents.detector_agent import detector_node
from src.agents.diagnoser_agent import diagnoser_node, llm_diagnoser
from src.agents.remediator_agent import remediator_node
from src.agents.communicator_agent import communicator_node
from src.agents.execution_agent import human_gate_node, execute_node
from src.agents.escalation_agent import escalate_node

from src.graph.routing import (
    route_after_diagnosis,
    route_after_remediator,
    route_after_verification
)

load_dotenv()

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_DB = str(DATA_DIR / "incident_state.db")


def build_graph() -> StateGraph:
    """
    put the langgraph nodes together
    """
    graph = StateGraph(AgentState)

    #register all nodes
    graph.add_node("detector", detector_node)
    graph.add_node("diagnoser", diagnoser_node)
    graph.add_node("llm_diagnoser", llm_diagnoser)
    graph.add_node("remediator", remediator_node)
    graph.add_node("communicator", communicator_node)
    graph.add_node("human_gate", human_gate_node)
    graph.add_node("execute", execute_node)
    graph.add_node("escalate", escalate_node)


    graph.set_entry_point("detector")

    # detector -> diagnoser (always proceed to diagnosis to identify root cause)
    graph.add_edge("detector", "diagnoser")

    # diagnoser -> route based on confidence
    graph.add_conditional_edges(
        "diagnoser",
        route_after_diagnosis,
        {
            "remediator": "remediator",
            "llm_diagnoser": "llm_diagnoser",
            "escalate": "escalate"
        }
    )

    # llm_diagnoser -> route based on confidence
    graph.add_conditional_edges(
        "llm_diagnoser",
        route_after_diagnosis,
        {
            "remediator": "remediator",
            "llm_diagnoser": "llm_diagnoser",
            "escalate": "escalate"
        }
    )

    #communicator runs in parallel with remediator
    #communicator is triggered by detector as well it does not wait for root cause
    graph.add_edge("detector", "communicator")
    graph.add_edge("communicator", END)

    # remediator -> human gate or execute
    graph.add_conditional_edges(
        "remediator",
        route_after_remediator,
        {
            "human_gate": "human_gate",
            "execute": "execute" 
        }
    )

    #human gate -> execute (graph resumes here after /approve call)
    graph.add_edge("human_gate", "execute")

    graph.add_conditional_edges(
        "execute",
        route_after_verification,
        {
            "end_resolved": END,
            "escalate_execution": "escalate"
        }
    )
    
    graph.add_edge("escalate", END)

    return graph


def compile():
    f"""
    compile graph with sqlite so we don't lose state if the server dies.
    also lets us pause for human approval.
    """
    
    import sqlite3
    graph = build_graph()
    conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    app = graph.compile(checkpointer=checkpointer, interrupt_before=["execute"])

    print(f"[orchestrator] Graph compiled. Checkpoint DB : {CHECKPOINT_DB}")
    return app


app = compile()

#run full incident through the graph
def run_incident(incident_id: str, raw_signals: dict, config: dict = None) -> dict:
    """
    wrapper to just run the whole incident through the graph.
    returns final state.
    """

    from src.graph.state import initial_state

    state = initial_state(incident_id, raw_signals)
    if config is None:
        config = {"configurable": {"thread_id": incident_id}}

    print(f"\n{'='*60}")
    print(f"INCIDENT: {incident_id}")
    print(f"ALERT: {raw_signals.get('alert_name', 'unknown')}")
    print(f"{'='*60}")

    result = app.invoke(state, config=config)

    print(f"\n{'='*60}")
    print(f"RESOLVED: {incident_id}")
    print(f"Status: {result.get('resolution_status')}")
    print(f"MTTR: {result.get('mttr_seconds')}s")
    print(f"Root cause: {result.get('root_cause', 'escalated')}")
    print(f"Diagnosis Summary: {result.get('diagnosis_summary')}")
    print(f"Evidence Summary: {result.get('evidence_summary')}")
    print(f"Blast Analysis: {result.get('blast_analysis')}")
    print(f"Suggested Remediation: {result.get('llm_suggested_action')}")
    print(f"Tokens: {result.get('total_tokens_used')} (${result.get('token_cost_usd', 0):.4f})")
    print(f"{'='*60}\n")

    return result