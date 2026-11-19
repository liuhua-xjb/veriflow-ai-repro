"""Independent recomputation, never trusts a tool's own 'valid' flag."""
import math
from decimal import Decimal, localcontext
from pydantic import ValidationError
from .tools import Summary, FieldResult
from .event_store import utc
from .workflow_models import digest, safe_value
from .verification_models import VerificationResult, FailureReason


def canonical(value):
    if isinstance(value, float) and not math.isfinite(value): return '[NONFINITE]'
    if isinstance(value, dict): return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, list): return [canonical(v) for v in value]
    return value


def fingerprint(value):
    return digest(canonical(value))


def preview(value, sensitive):
    return '[REDACTED]' if sensitive else safe_value(canonical(value))


class ValidatorRegistry:
    version = '1.0.0'

    def verify(self, tool, arguments, output, policy, sensitive=False):
        results = []
        def add(rule, code=None, path='$', recoverable=True, difference=None):
            result = VerificationResult(rule_id=rule, passed=code is None,
                failure=FailureReason(code=code, path=path, recoverable=recoverable) if code else None,
                difference={} if sensitive else (difference or {}), input_digest=fingerprint(arguments),
                output_digest=fingerprint(output), timestamp=utc())
            results.append(result.model_dump(mode='json'))
        try:
            model = Summary if tool == 'numeric_summary' else FieldResult
            model.model_validate(output)
            if tool == 'numeric_summary':
                if not (1 <= output['count'] <= 1000 and all(math.isfinite(output[k]) and abs(output[k]) <= 1e100 for k in ('minimum','maximum','mean'))
                        and output['minimum'] <= output['mean'] <= output['maximum']): raise ValueError()
            elif output['missing'] != sorted(set(output['missing'])): raise ValueError()
            add('output_schema')
        except (ValidationError, ValueError, TypeError, KeyError):
            add('output_schema', 'schema_invalid')
            return results
        if tool == 'numeric_summary':
            with localcontext() as context:
                context.prec = 460
                values = [Decimal(str(v)) for v in arguments['values']]
                expected = {'count': len(values), 'minimum': min(values), 'maximum': max(values), 'mean': sum(values)/len(values)}
                for key, target in expected.items():
                    actual = Decimal(str(output[key]))
                    delta = abs(actual - target)
                    tolerance = max(Decimal(str(policy.abs_tolerance)), abs(target)*Decimal(str(policy.rel_tolerance))) if key == 'mean' else Decimal(0)
                    if delta > tolerance:
                        add('numeric_recompute', 'numeric_mismatch', key, difference={'expected':float(target), 'actual':float(actual), 'absolute_error':float(delta)})
                        break
                else: add('numeric_recompute')
        else:
            missing = sorted(set(arguments['required']) - arguments['document'].keys())
            add('json_fields_recompute', None if output == {'valid':not missing,'missing':missing} else 'field_result_mismatch', 'missing/valid')
            add('required_fields', 'required_fields_missing' if missing else None, 'document', False)
        return results


validators = ValidatorRegistry()
