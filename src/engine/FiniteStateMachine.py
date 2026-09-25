import time

# Local import
from src.states.base_state import State
from src.utils.logger import logger

class FiniteStateMachine:
    '''
    Finite State Machine
    '''
    def __init__(self):
        self.states = {}              # name -> State instance
        self.transitions = {}         # name -> set of legal next names
        self.state = None
        self.t_last_transition = time.time()

    def add_state(self, state: State):
        self.states[state.name] = state
        self.transitions[state.name] = set()

    def set_init_state(self, state_name):
        if state_name in self.states:
            self.state = self.states[state_name]
            self.state.on_enter()
            self.t_last_transition = time.time()
        else:
            logger.error(f"[set_init_state] Unexpected state: {state_name}")

    def transit_to(self, to_state_name):
        # Ignore frequent transition
        dt = time.time() - self.t_last_transition
        if dt < 1:
            return

        if to_state_name in self.transitions[self.state.name]:
            logger.info(f"[FSM] Stayed in {self.state.name} state for {round(dt, 2)} seconds.")
            logger.info(f"[FSM] transit from {self.state.name} to {to_state_name}")
            self.state.on_exit()
            self.state = self.states[to_state_name]
            self.state.on_enter()
            self.t_last_transition = time.time()

    def do_state_stuff(self):
        # Do state stuff per-frame
        self.state.on_frame()

        # Check if need transition
        to_state = self.state.check_transitions()
        if to_state is not None:
            self.transit_to(to_state)
