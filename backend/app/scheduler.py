"""Process-local admission and deterministic scoring, shared across all runs."""
import threading


class Scheduler:
    def __init__(self):
        self.lock = threading.RLock()
        self.loads = {}  # agent id -> active snapshot concurrency limits
        self.active = 0

    def choose(self, node, agents, strategy, config, age=0, reserve=False):
        with self.lock:
            candidates = []
            for agent in sorted(agents, key=lambda a: a.id):
                reasons = []
                if not agent.enabled: reasons.append('disabled')
                if node.agent_id and node.agent_id != agent.id: reasons.append('agent_constraint')
                if node.capability not in agent.capabilities: reasons.append('capability_mismatch')
                if node.tool not in agent.allowed_tools: reasons.append('tool_permission_denied')
                load = len(self.loads.get(agent.id, []))
                cap = min([agent.concurrency_limit] + self.loads.get(agent.id, []))
                if load >= cap: reasons.append('concurrency_limit')
                score = (config.capability_weight + config.priority_weight * node.priority + config.age_weight * age -
                         config.load_weight * load / agent.concurrency_limit)
                candidates.append({'agent_id': agent.id, 'version': agent.version, 'load': load,
                                   'capacity': cap, 'score': round(score, 6), 'excluded': reasons})
            available = [c for c in candidates if not c['excluded']]
            selected = (sorted(available, key=lambda c: (-c['score'], c['agent_id']))[0] if strategy == 'dynamic' else available[0]) if available else None
            if self.active >= 4: selected = None
            if selected and reserve:
                agent = next(a for a in agents if a.id == selected['agent_id'])
                self.loads.setdefault(agent.id, []).append(agent.concurrency_limit)
                self.active += 1
            return selected, candidates

    def release(self, agent):
        with self.lock:
            self.loads[agent.id].remove(agent.concurrency_limit)
            if not self.loads[agent.id]: del self.loads[agent.id]
            self.active -= 1
