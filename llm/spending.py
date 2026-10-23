"""Per-run pre-request reservation for default paid providers (no persistence).

Known price × conservative token bound; not a provider invoice or account quota.
An unanswered call retains its full reservation, including truncation/repair.
"""
import json
import math
import re

from llm.errors import BudgetExceeded
from observability.pricing import PRICES_PER_MTOK


class SpendingLimit:
    def __init__(self, cap):
        if not math.isfinite(cap) or cap < 0:
            raise ValueError("finite nonnegative run budget required")
        self.cap, self.accounted = cap, 0.0

    def reserve(self, req, model):
        known = next((m for m in sorted(PRICES_PER_MTOK, key=len, reverse=True)
                      if model == m or re.fullmatch(re.escape(m)+r"-\d{4}-\d{2}-\d{2}",model)), None)
        if known is None or known == "mock":
            raise BudgetExceeded("model price is not bounded; paid request withheld")
        schema = json.dumps(req.schema.model_json_schema(),ensure_ascii=False) if req.schema else ""
        size = len((req.system+req.user+schema).encode("utf-8"))
        if size > 180000 or not 0 < req.max_tokens <= 8192 or len(req.images)>2 or any(i.size>4000000 for i in req.images):
            raise BudgetExceeded("request exceeds priced size bounds")
        pin,pout = PRICES_PER_MTOK[known]
        tokens = size+4096+32768*len(req.images)
        amount = (tokens*pin+req.max_tokens*pout)/1000000
        if self.accounted+amount > self.cap+1e-12:
            raise BudgetExceeded("run budget would be exceeded; request not sent")
        self.accounted += amount
        return amount,tokens,pin,pout

    def settle(self, reservation, req, model, response):
        amount,tokens,pin,pout = reservation
        same = response.model == model or bool(re.fullmatch(re.escape(model)+r"-\d{4}-\d{2}-\d{2}",response.model))
        if (not same or response.cost_estimated or type(response.input_tokens) is not int or type(response.output_tokens) is not int
                or not 0 <= response.input_tokens <= tokens or not 0 <= response.output_tokens <= req.max_tokens):
            raise BudgetExceeded("unverified model or usage; reservation retained")
        actual=(response.input_tokens*pin+response.output_tokens*pout)/1000000
        self.accounted += actual-amount
