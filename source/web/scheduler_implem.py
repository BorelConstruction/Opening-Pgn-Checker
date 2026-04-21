from typing import TypeAlias

from source.web.scheduler_protocol import SessionLog

SpecId : TypeAlias = str

class NaiveScheduler:
    def __init__(self, log: SessionLog):
        self.log = log
        
    def next(self) -> SpecId:
        return "new"
    
    def feedback(self, spec_id, feedback) -> None:
        pass