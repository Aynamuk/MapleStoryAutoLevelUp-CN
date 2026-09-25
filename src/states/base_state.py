class State:
    def __init__(self, name, bot):
        self.name = name # 状态名（巡逻模式移除后，目前只有 "hunting"）
        self.bot = bot  # reference to MapleStoryAutoLevelUp

    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def check_transitions(self):
        pass

    def do_state_stuff(self):
        pass
