"""Reads axial PDF shadings so they can be written as SVG gradients."""

import re


class ShadingCollector:
    """Walks a page content stream and records every `sh` with its matrix and blend state."""

    def __init__(self, doc, page):
        self.doc = doc
        self.shadings = []
        resources = doc.xref_get_key(page.xref, "Resources")
        contents = b"".join(doc.xref_stream(xref) for xref in page.get_contents())
        self.run(contents, self.resources_xref(page.xref, resources), [1, 0, 0, 1, 0, 0], "Normal", 1.0)

    def resources_xref(self, owner, value):
        kind, text = value
        if kind == "xref":
            return int(text.split()[0])
        if kind == "dict":
            return ("inline", owner)
        return None

    def resource(self, resources, category, name):
        if resources is None:
            return None
        if isinstance(resources, tuple):
            kind, text = self.doc.xref_get_key(resources[1], f"Resources/{category}/{name}")
        else:
            kind, text = self.doc.xref_get_key(resources, f"{category}/{name}")
        return int(text.split()[0]) if kind == "xref" else None

    def run(self, stream, resources, ctm, group_blend, group_alpha):
        stack = []
        state = {"ctm": ctm, "blend": "Normal", "alpha": 1.0}
        operands = []
        for token in tokenize(stream):
            if not is_operator(token):
                operands.append(token)
                continue
            if token == "q":
                stack.append(dict(state))
            elif token == "Q" and stack:
                state = stack.pop()
            elif token == "cm":
                state["ctm"] = multiply([float(value) for value in operands[-6:]], state["ctm"])
            elif token == "gs":
                xref = self.resource(resources, "ExtGState", operands[-1].lstrip("/"))
                if xref:
                    mode = self.doc.xref_get_key(xref, "BM")[1].lstrip("/")
                    alpha = self.doc.xref_get_key(xref, "ca")[1]
                    state["blend"] = mode if mode and mode != "null" else "Normal"
                    state["alpha"] = float(alpha) if alpha not in ("null", "") else 1.0
            elif token == "Do":
                xref = self.resource(resources, "XObject", operands[-1].lstrip("/"))
                if xref and self.doc.xref_get_key(xref, "Subtype")[1] == "/Form":
                    matrix = self.doc.xref_get_key(xref, "Matrix")
                    form = [float(v) for v in matrix[1].strip("[]").split()] if matrix[0] == "array" else [1, 0, 0, 1, 0, 0]
                    # A form is composited as a group with the state that draws it.
                    blend = state["blend"] if state["blend"] != "Normal" else group_blend
                    inner = self.resources_xref(xref, self.doc.xref_get_key(xref, "Resources")) or resources
                    self.run(self.doc.xref_stream(xref), inner, multiply(form, state["ctm"]),
                             blend, group_alpha * state["alpha"])
            elif token == "sh":
                xref = self.resource(resources, "Shading", operands[-1].lstrip("/"))
                blend = state["blend"] if state["blend"] != "Normal" else group_blend
                self.shadings.append(self.read_shading(xref, state["ctm"], blend, group_alpha * state["alpha"]))
            operands = []

    def read_shading(self, xref, ctm, blend, alpha):
        if self.doc.xref_get_key(xref, "ShadingType")[1] != "2":
            raise SystemExit(f"Unsupported shading type in object {xref}")
        coords = [float(value) for value in self.doc.xref_get_key(xref, "Coords")[1].strip("[]").split()]
        function = int(self.doc.xref_get_key(xref, "Function")[1].split()[0])
        return {"matrix": ctm, "coords": coords, "stops": self.stops(function, 0, 1), "blend": blend, "alpha": alpha}

    def stops(self, xref, start, end):
        kind = self.doc.xref_get_key(xref, "FunctionType")[1]
        if kind == "2":
            c0 = numbers(self.doc.xref_get_key(xref, "C0")[1]) or [0]
            c1 = numbers(self.doc.xref_get_key(xref, "C1")[1]) or [1]
            return [(start, c0), (end, c1)]
        if kind == "3":
            functions = [int(ref) for ref in re.findall(r"(\d+) 0 R", self.doc.xref_get_key(xref, "Functions")[1])]
            bounds = [start] + numbers(self.doc.xref_get_key(xref, "Bounds")[1]) + [end]
            result = []
            for index, function in enumerate(functions):
                result += self.stops(function, bounds[index], bounds[index + 1])
            return result
        raise SystemExit(f"Unsupported function type {kind} in object {xref}")


def numbers(text):
    return [float(value) for value in re.findall(r"-?\d*\.?\d+", text or "")]


def multiply(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return [a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2]


def tokenize(stream):
    text = stream.decode("latin-1")
    pattern = r"\((?:\\.|[^\\)])*\)|<<|>>|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/\[\]()<>{}%]+|[^\s/\[\]()<>{}%]+"
    return re.findall(pattern, re.sub(r"%[^\r\n]*", "", text))


def is_operator(token):
    return re.fullmatch(r"[A-Za-z'\"*]+[0-9]?\*?", token) is not None and token not in ("true", "false", "null")
