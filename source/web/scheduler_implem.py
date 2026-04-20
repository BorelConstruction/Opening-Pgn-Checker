from typing import TypeAlias

SpecId : TypeAlias = str

class NaiveScheduler:
    def next(self) -> SpecId:
        return "new"
    
    def feedback(self) -> None:
        pass