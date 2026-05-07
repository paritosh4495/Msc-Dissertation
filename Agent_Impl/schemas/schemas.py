
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Dict
from pydantic import BaseModel, Field

# 1. Tool Response 

class ToolResponse(BaseModel):
    """
    Standard envelope returned by every diagnostic tool.
    Every tool returns a ToolResponse — never raises an exception to the agent.
    Failures are communicated via status='error' and error_message.
    """
    tool:          str = Field(..., description="Name of the tool that generated this response.")
    status:        Literal["success", "error"] = Field(..., description=(
        "Execution status."
    ))
    data:          dict[str, Any]               = Field(default_factory=dict, description="Tool output payload.")
    service:       Optional[str]                = None
    timestamp_utc: str                          = Field(
                       default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                   )
    error_message: Optional[str]                = None

    model_config = {"frozen": True}

# 2. Diagnosis Submission

class DiagnosisSubmission(BaseModel):
    """
    Validated terminal output of the agent — input to submit_diagnosis tool.
    Scored against ground truth immediately after session completion.
    """
    service:    Literal["inventory-service", "order-service", "payment-service"] = Field(..., description=(
        "Root cause service."
    ))
    component:  Literal["hikari-connection-pool", "cpu", "resilience4j-circuit-breaker", "tomcat-thread-pool", "jvm-heap", "kubernetes-pod"] = Field(..., description=(
        "Specific component at fault"
    ))
    fault_type: Literal["connection-pool-starvation", "cpu-saturation", "circuit-breaker-open", "thread-pool-exhaustion", "memory-leak", "pod-oomkill"] = Field(..., description=(
        "Fault classification"
    ))
    evidence:   str = Field(..., min_length=10, description=(
        "Concise summary of evidence supporting this diagnosis."
    ))
    no_fault_detected: bool = Field(default=False, description=(
        "True if the agent concluded no fault exists in the sytesm after thorough investigation. When True, service/component/fault_type reflect the agent's best guess or are irrelevant — the key signal is no_fault_detected=True."
    ))

# # 3. Scoring Result

# class ScoringResult(BaseModel):
#     """
#     Produced once per trial by scoring the agent's DiagnosisSubmission
#     against ground truth. All derived fields are computed in model_post_init.
#     """
#     service_correct:    bool
#     component_correct:  bool
#     fault_type_correct: bool
#     submitted:          bool
#     step_limit_reached: bool

#     # Derived — populated in model_post_init, not passed by caller.
#     exact_correct:    bool            = Field(default=False)
#     partial_score:    float           = Field(default=0.0)
#     outcome_category: str             = Field(default="")

#     def model_post_init(self, __context: Any) -> None:
#         object.__setattr__(self, "exact_correct", (
#             self.service_correct and self.component_correct and self.fault_type_correct
#         ))
#         object.__setattr__(self, "partial_score", round(
#             (self.service_correct + self.component_correct + self.fault_type_correct) / 3.0, 4
#         ))
#         object.__setattr__(self, "outcome_category", self._derive_category())

#     def _derive_category(self) -> str:
#         if not self.submitted or self.step_limit_reached:
#             return "STEP_LIMIT_REACHED" if self.step_limit_reached else "NO_SUBMISSION"
#         if self.exact_correct:
#             return "EXACT_CORRECT"
#         if not self.service_correct:
#             return "WRONG"
#         if self.component_correct ^ self.fault_type_correct:
#             return "PARTIAL"
#         return "SERVICE_ONLY"

#     model_config = {"frozen": True}