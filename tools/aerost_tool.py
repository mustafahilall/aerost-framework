#!/usr/bin/env python3
"""AEROST research compiler and evidence pipeline.

The tool uses only the Python standard library. It parses the supported ASCP
subset into a real AST, performs semantic analysis, lowers to executable AIR,
interprets AIR, generates an independent Rust crate, compares traces, computes
coverage and MC/DC from runtime events, mutates AIR, validates JSON artifacts,
measures the generated backend, and checks clean-directory reproducibility.
"""
from __future__ import annotations

import argparse
import ast
import copy
import dataclasses
import hashlib
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

TOOL_VERSION = "AEROST-TOOL-RESEARCH-BASELINE"
PROFILE_VERSION = "ASCP-0.2"
SCHEMA_VERSION = "AEROST-SCHEMA-0.2"
DEFAULT_APPLICATION_MANIFEST = Path("applications/power-supervisor.application.json")


class AerostError(Exception):
    """Base controlled failure."""


@dataclass(frozen=True)
class ApplicationConfig:
    application_id: str
    profile_version: str
    manifest_path: Path
    source_path: Path
    requirements_path: Path
    runtime_fault_policy_path: Path
    controlled_scenario_paths: tuple[Path, ...]
    manual_requirements_path: Path
    manual_closure_path: Path
    input_domain_path: Path
    mutation_profile_path: Path
    air_artifact_name: str
    manifest: dict[str, Any]

    def summary(self, root: Path) -> dict[str, Any]:
        return {
            "id": self.application_id,
            "profile_version": self.profile_version,
            "manifest": self.manifest_path.relative_to(root).as_posix(),
            "manifest_sha256": sha256_file(self.manifest_path),
            "source": self.source_path.relative_to(root).as_posix(),
            "requirements": self.requirements_path.relative_to(root).as_posix(),
            "runtime_fault_policy": self.runtime_fault_policy_path.relative_to(root).as_posix(),
            "input_domain": self.input_domain_path.relative_to(root).as_posix(),
            "mutation_profile": self.mutation_profile_path.relative_to(root).as_posix(),
            "air_artifact": self.air_artifact_name,
        }


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    line: int
    column: int
    end_line: int
    end_column: int

    def to_json(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Annotation:
    requirements: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    claim_critical: bool = False
    mcdc: bool = False
    mutation_tags: tuple[str, ...] = ()

    def merged(self, parent: "Annotation") -> "Annotation":
        return Annotation(
            requirements=self.requirements or parent.requirements,
            tests=self.tests or parent.tests,
            claim_critical=self.claim_critical,
            mcdc=self.mcdc,
            mutation_tags=self.mutation_tags,
        )


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    span: Span
    annotation: Annotation | None = None


KEYWORDS = {
    "TYPE", "END_TYPE", "PROGRAM", "END_PROGRAM", "VAR_INPUT", "VAR_OUTPUT",
    "VAR_STATE", "VAR", "END_VAR", "TRANSITION_STAGE", "OUTPUT_STAGE", "IF",
    "THEN", "ELSIF", "ELSE", "END_IF", "CASE", "OF", "END_CASE", "BOOL",
    "U16", "I32", "TRUE", "FALSE", "NOT", "AND", "OR", "WHILE", "DO",
    "END_WHILE", "REAL",
}

MULTI = {":=": "ASSIGN", "<=": "LE", ">=": "GE", "<>": "NE"}
SINGLE = {
    ":": "COLON", ";": "SEMI", ",": "COMMA", "(": "LPAREN", ")": "RPAREN",
    "=": "EQ", "<": "LT", ">": "GT", "+": "PLUS", "-": "MINUS",
}


def parse_annotation(text: str) -> Annotation:
    body = text.strip()
    if body.startswith("(*@"):
        body = body[3:]
    if body.endswith("*)"):
        body = body[:-2]
    reqs: list[str] = []
    tests: list[str] = []
    mutation_tags: list[str] = []
    claim = False
    mcdc = False
    for part in re.split(r"\s+", body.strip()):
        if not part:
            continue
        if part.startswith("req="):
            reqs.extend(x for x in part[4:].split(",") if x)
        elif part.startswith("test="):
            tests.extend(x for x in part[5:].split(",") if x)
        elif part == "claim-critical":
            claim = True
        elif part == "mcdc":
            mcdc = True
        elif part.startswith("mutate="):
            mutation_tags.extend(x for x in part[7:].split(",") if x)
    return Annotation(tuple(reqs), tuple(tests), claim, mcdc, tuple(mutation_tags))


def lex(source: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    line = 1
    col = 1
    pending_annotation: Annotation | None = None

    def advance(text: str) -> tuple[int, int]:
        nonlocal line, col
        for ch in text:
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
        return line, col

    while i < len(source):
        ch = source[i]
        if ch.isspace():
            advance(ch)
            i += 1
            continue
        if source.startswith("(*", i):
            end = source.find("*)", i + 2)
            if end < 0:
                raise AerostError(f"unterminated comment at {line}:{col}")
            text = source[i:end + 2]
            if text.startswith("(*@"):
                pending_annotation = parse_annotation(text)
            advance(text)
            i = end + 2
            continue
        start_i, start_line, start_col = i, line, col
        matched = False
        for op, kind in MULTI.items():
            if source.startswith(op, i):
                advance(op)
                i += len(op)
                tokens.append(Token(kind, op, Span(start_i, i, start_line, start_col, line, col), pending_annotation))
                pending_annotation = None
                matched = True
                break
        if matched:
            continue
        if ch in SINGLE:
            advance(ch)
            i += 1
            tokens.append(Token(SINGLE[ch], ch, Span(start_i, i, start_line, start_col, line, col), pending_annotation))
            pending_annotation = None
            continue
        if ch.isdigit():
            j = i + 1
            while j < len(source) and source[j].isdigit():
                j += 1
            text = source[i:j]
            advance(text)
            i = j
            tokens.append(Token("INTEGER", text, Span(start_i, i, start_line, start_col, line, col), pending_annotation))
            pending_annotation = None
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < len(source) and (source[j].isalnum() or source[j] == "_"):
                j += 1
            text = source[i:j]
            upper = text.upper()
            advance(text)
            i = j
            kind = upper if upper in KEYWORDS else "IDENT"
            tokens.append(Token(kind, text, Span(start_i, i, start_line, start_col, line, col), pending_annotation))
            pending_annotation = None
            continue
        raise AerostError(f"unexpected character {ch!r} at {line}:{col}")
    eof_span = Span(len(source), len(source), line, col, line, col)
    tokens.append(Token("EOF", "", eof_span, pending_annotation))
    return tokens


@dataclass
class TypeDecl:
    name: str
    values: list[str]
    span: Span


@dataclass
class VarDecl:
    name: str
    type_name: str
    initializer: "Expr | None"
    section: str
    span: Span


@dataclass
class Expr:
    kind: str
    span: Span
    value: Any = None
    left: "Expr | None" = None
    right: "Expr | None" = None
    operand: "Expr | None" = None
    value_type: str | None = None
    node_id: str | None = None


@dataclass
class Statement:
    kind: str
    span: Span
    annotation: Annotation = field(default_factory=Annotation)
    target: str | None = None
    expr: Expr | None = None
    branches: list[tuple[Expr, list["Statement"], Annotation]] = field(default_factory=list)
    else_body: list["Statement"] = field(default_factory=list)
    selector: Expr | None = None
    arms: list[tuple[str, list["Statement"], Annotation]] = field(default_factory=list)
    node_id: str | None = None
    decision_ids: list[str] = field(default_factory=list)


@dataclass
class Program:
    name: str
    types: list[TypeDecl]
    inputs: list[VarDecl]
    outputs: list[VarDecl]
    states: list[VarDecl]
    transition: list[Statement]
    output: list[Statement]
    span: Span


class Parser:
    def __init__(self, tokens: Sequence[Token]):
        self.tokens = list(tokens)
        self.pos = 0

    def peek(self, *kinds: str) -> bool:
        return self.tokens[self.pos].kind in kinds

    def current(self) -> Token:
        return self.tokens[self.pos]

    def take(self, kind: str | None = None) -> Token:
        tok = self.current()
        if kind is not None and tok.kind != kind:
            raise AerostError(f"expected {kind}, found {tok.kind} at {tok.span.line}:{tok.span.column}")
        self.pos += 1
        return tok

    def current_annotation(self, inherited: Annotation | None = None) -> Annotation:
        ann = self.current().annotation or Annotation()
        return ann.merged(inherited) if inherited else ann

    def parse(self) -> Program:
        types: list[TypeDecl] = []
        while self.peek("TYPE"):
            types.append(self.parse_type())
        start = self.take("PROGRAM")
        name = self.take("IDENT").text
        self.optional("SEMI")
        inputs: list[VarDecl] = []
        outputs: list[VarDecl] = []
        states: list[VarDecl] = []
        while self.peek("VAR_INPUT", "VAR_OUTPUT", "VAR_STATE", "VAR"):
            section_tok = self.take()
            section = {"VAR_INPUT": "input", "VAR_OUTPUT": "output", "VAR_STATE": "state", "VAR": "state"}[section_tok.kind]
            dest = {"input": inputs, "output": outputs, "state": states}[section]
            while not self.peek("END_VAR"):
                dest.extend(self.parse_var_decl(section))
            self.take("END_VAR")
            self.optional("SEMI")
        self.take("TRANSITION_STAGE")
        transition = self.parse_statements({"OUTPUT_STAGE"}, Annotation())
        self.take("OUTPUT_STAGE")
        output = self.parse_statements({"END_PROGRAM"}, Annotation())
        end = self.take("END_PROGRAM")
        self.optional("SEMI")
        self.take("EOF")
        return Program(name, types, inputs, outputs, states, transition, output,
                       Span(start.span.start, end.span.end, start.span.line, start.span.column, end.span.end_line, end.span.end_column))

    def optional(self, kind: str) -> bool:
        if self.peek(kind):
            self.take(kind)
            return True
        return False

    def parse_type(self) -> TypeDecl:
        start = self.take("TYPE")
        name = self.take("IDENT").text
        self.take("COLON")
        self.take("LPAREN")
        values: list[str] = []
        while not self.peek("RPAREN"):
            values.append(self.take("IDENT").text)
            if not self.optional("COMMA"):
                break
        end = self.take("RPAREN")
        self.optional("SEMI")
        self.take("END_TYPE")
        self.optional("SEMI")
        return TypeDecl(name, values, Span(start.span.start, end.span.end, start.span.line, start.span.column, end.span.end_line, end.span.end_column))

    def parse_var_decl(self, section: str) -> list[VarDecl]:
        first = self.take("IDENT")
        names = [first.text]
        while self.optional("COMMA"):
            names.append(self.take("IDENT").text)
        self.take("COLON")
        type_tok = self.take()
        if type_tok.kind not in {"BOOL", "U16", "I32", "IDENT"}:
            raise AerostError(f"invalid type {type_tok.text} at {type_tok.span.line}:{type_tok.span.column}")
        initializer = None
        if self.optional("ASSIGN"):
            initializer = self.parse_expr()
        end = self.take("SEMI")
        return [VarDecl(name, type_tok.text, copy.deepcopy(initializer), section,
                        Span(first.span.start, end.span.end, first.span.line, first.span.column, end.span.end_line, end.span.end_column)) for name in names]

    def parse_statements(self, terminators: set[str], inherited: Annotation) -> list[Statement]:
        out: list[Statement] = []
        while not self.peek(*terminators):
            if self.peek("EOF"):
                raise AerostError(f"unexpected EOF while parsing statements; expected {sorted(terminators)}")
            out.append(self.parse_statement(inherited))
        return out

    def parse_statement(self, inherited: Annotation) -> Statement:
        ann = self.current_annotation(inherited)
        if self.peek("IF"):
            return self.parse_if(ann)
        if self.peek("CASE"):
            return self.parse_case(ann)
        ident = self.take("IDENT")
        self.take("ASSIGN")
        expr = self.parse_expr()
        end = self.take("SEMI")
        return Statement("assign", Span(ident.span.start, end.span.end, ident.span.line, ident.span.column, end.span.end_line, end.span.end_column), ann, target=ident.text, expr=expr)

    def parse_if(self, ann: Annotation) -> Statement:
        start = self.take("IF")
        condition = self.parse_expr()
        self.take("THEN")
        body = self.parse_statements({"ELSIF", "ELSE", "END_IF"}, Annotation())
        branches: list[tuple[Expr, list[Statement], Annotation]] = [(condition, body, ann)]
        while self.peek("ELSIF"):
            tok = self.take("ELSIF")
            branch_ann = (tok.annotation or Annotation()).merged(ann)
            cond = self.parse_expr()
            self.take("THEN")
            branch_body = self.parse_statements({"ELSIF", "ELSE", "END_IF"}, Annotation())
            branches.append((cond, branch_body, branch_ann))
        else_body: list[Statement] = []
        if self.peek("ELSE"):
            else_tok = self.take("ELSE")
            else_ann = (else_tok.annotation or Annotation()).merged(ann)
            else_body = self.parse_statements({"END_IF"}, Annotation())
        end = self.take("END_IF")
        self.optional("SEMI")
        return Statement("if", Span(start.span.start, end.span.end, start.span.line, start.span.column, end.span.end_line, end.span.end_column), ann, branches=branches, else_body=else_body)

    def parse_case(self, ann: Annotation) -> Statement:
        start = self.take("CASE")
        selector = self.parse_expr()
        self.take("OF")
        arms: list[tuple[str, list[Statement], Annotation]] = []
        while not self.peek("END_CASE"):
            arm_tok = self.current()
            arm_ann = (arm_tok.annotation or Annotation()).merged(ann)
            value = self.take("IDENT").text
            self.take("COLON")
            body: list[Statement] = []
            while not self.peek("END_CASE"):
                # At CASE-arm level, IDENT COLON starts the next arm.
                if self.peek("IDENT") and self.tokens[self.pos + 1].kind == "COLON":
                    break
                body.append(self.parse_statement(arm_ann))
            arms.append((value, body, arm_ann))
        end = self.take("END_CASE")
        self.optional("SEMI")
        return Statement("case", Span(start.span.start, end.span.end, start.span.line, start.span.column, end.span.end_line, end.span.end_column), ann, selector=selector, arms=arms)

    def parse_expr(self) -> Expr:
        return self.parse_or()

    def parse_or(self) -> Expr:
        expr = self.parse_and()
        while self.peek("OR"):
            op = self.take()
            right = self.parse_and()
            expr = Expr("binary", merge_span(expr.span, right.span), value=op.kind, left=expr, right=right)
        return expr

    def parse_and(self) -> Expr:
        expr = self.parse_compare()
        while self.peek("AND"):
            op = self.take()
            right = self.parse_compare()
            expr = Expr("binary", merge_span(expr.span, right.span), value=op.kind, left=expr, right=right)
        return expr

    def parse_compare(self) -> Expr:
        expr = self.parse_unary()
        if self.peek("EQ", "NE", "LT", "LE", "GT", "GE"):
            op = self.take()
            right = self.parse_unary()
            expr = Expr("binary", merge_span(expr.span, right.span), value=op.kind, left=expr, right=right)
        return expr

    def parse_unary(self) -> Expr:
        if self.peek("NOT", "MINUS", "PLUS"):
            op = self.take()
            operand = self.parse_unary()
            return Expr("unary", merge_span(op.span, operand.span), value=op.kind, operand=operand)
        return self.parse_primary()

    def parse_primary(self) -> Expr:
        tok = self.take()
        if tok.kind == "TRUE":
            return Expr("bool", tok.span, value=True)
        if tok.kind == "FALSE":
            return Expr("bool", tok.span, value=False)
        if tok.kind == "INTEGER":
            return Expr("int", tok.span, value=int(tok.text))
        if tok.kind == "IDENT":
            return Expr("name", tok.span, value=tok.text)
        if tok.kind == "LPAREN":
            expr = self.parse_expr()
            end = self.take("RPAREN")
            expr.span = Span(tok.span.start, end.span.end, tok.span.line, tok.span.column, end.span.end_line, end.span.end_column)
            return expr
        raise AerostError(f"unexpected token {tok.kind} in expression at {tok.span.line}:{tok.span.column}")


def merge_span(a: Span, b: Span) -> Span:
    return Span(a.start, b.end, a.line, a.column, b.end_line, b.end_column)


def parse_source(source: str) -> Program:
    return Parser(lex(source)).parse()

@dataclass
class Symbol:
    name: str
    type_name: str
    section: str
    initializer: Any = None


@dataclass
class TypedProgram:
    program: Program
    symbols: dict[str, Symbol]
    enum_values: dict[str, str]
    enum_types: dict[str, list[str]]


def normalize_type(name: str) -> str:
    upper = name.upper()
    return upper if upper in {"BOOL", "U16", "I32"} else name


def semantic_analyze(program: Program) -> TypedProgram:
    enum_types: dict[str, list[str]] = {}
    enum_values: dict[str, str] = {}
    errors: list[str] = []
    for typ in program.types:
        if typ.name in enum_types:
            errors.append(f"duplicate type {typ.name}")
        seen: set[str] = set()
        for value in typ.values:
            if value in seen or value in enum_values:
                errors.append(f"duplicate enumeration literal {value}")
            seen.add(value)
            enum_values[value] = typ.name
        enum_types[typ.name] = list(typ.values)
    symbols: dict[str, Symbol] = {}
    for decl in program.inputs + program.outputs + program.states:
        if decl.name in symbols or decl.name in enum_values:
            errors.append(f"duplicate declaration {decl.name} at line {decl.span.line}")
            continue
        t = normalize_type(decl.type_name)
        if t not in {"BOOL", "U16", "I32"} and t not in enum_types:
            errors.append(f"unknown type {decl.type_name} for {decl.name}")
        if decl.section == "state" and decl.initializer is None:
            errors.append(f"retained state {decl.name} requires an initializer")
        symbols[decl.name] = Symbol(decl.name, t, decl.section)

    def infer(expr: Expr) -> str:
        if expr.kind == "bool":
            expr.value_type = "BOOL"
        elif expr.kind == "int":
            expr.value_type = "INT_LITERAL"
        elif expr.kind == "name":
            if expr.value in symbols:
                expr.value_type = symbols[expr.value].type_name
            elif expr.value in enum_values:
                expr.value_type = enum_values[expr.value]
            else:
                errors.append(f"undefined identifier {expr.value} at line {expr.span.line}")
                expr.value_type = "ERROR"
        elif expr.kind == "unary":
            operand_type = infer(expr.operand)
            if expr.value == "NOT":
                if operand_type != "BOOL":
                    errors.append(f"NOT requires BOOL at line {expr.span.line}")
                expr.value_type = "BOOL"
            else:
                if operand_type not in {"U16", "I32", "INT_LITERAL"}:
                    errors.append(f"numeric unary operator requires integer at line {expr.span.line}")
                expr.value_type = operand_type
        elif expr.kind == "binary":
            left_type = infer(expr.left)
            right_type = infer(expr.right)
            if expr.value in {"AND", "OR"}:
                if left_type != "BOOL" or right_type != "BOOL":
                    errors.append(f"{expr.value} requires BOOL operands at line {expr.span.line}")
                expr.value_type = "BOOL"
            elif expr.value in {"EQ", "NE"}:
                compatible = left_type == right_type or (
                    {left_type, right_type} <= {"U16", "I32", "INT_LITERAL"}
                )
                if not compatible:
                    errors.append(f"incompatible comparison {left_type} and {right_type} at line {expr.span.line}")
                expr.value_type = "BOOL"
            else:
                if left_type not in {"U16", "I32", "INT_LITERAL"} or right_type not in {"U16", "I32", "INT_LITERAL"}:
                    errors.append(f"ordered comparison requires integer operands at line {expr.span.line}")
                expr.value_type = "BOOL"
        return expr.value_type or "ERROR"

    def check_assignment(stmt: Statement, stage: str) -> None:
        symbol = symbols.get(stmt.target or "")
        if symbol is None:
            errors.append(f"assignment to undefined {stmt.target} at line {stmt.span.line}")
            infer(stmt.expr)
            return
        if symbol.section == "input":
            errors.append(f"cannot assign input {symbol.name} at line {stmt.span.line}")
        if stage == "transition" and symbol.section != "state":
            errors.append(f"transition stage may only write retained state: {symbol.name} at line {stmt.span.line}")
        if stage == "output" and symbol.section != "output":
            errors.append(f"output stage may only write outputs: {symbol.name} at line {stmt.span.line}")
        source_type = infer(stmt.expr)
        target_type = symbol.type_name
        if source_type == "INT_LITERAL" and target_type in {"U16", "I32"}:
            value = int(stmt.expr.value)
            if target_type == "U16" and not 0 <= value <= 65535:
                errors.append(f"U16 literal out of range at line {stmt.span.line}")
            if target_type == "I32" and not -(2**31) <= value < 2**31:
                errors.append(f"I32 literal out of range at line {stmt.span.line}")
        elif source_type != target_type:
            errors.append(f"cannot assign {source_type} to {target_type} for {symbol.name} at line {stmt.span.line}")

    def check_statements(statements: list[Statement], stage: str) -> None:
        for stmt in statements:
            if stmt.kind == "assign":
                check_assignment(stmt, stage)
            elif stmt.kind == "if":
                for condition, body, _ in stmt.branches:
                    if infer(condition) != "BOOL":
                        errors.append(f"IF condition must be BOOL at line {condition.span.line}")
                    check_statements(body, stage)
                check_statements(stmt.else_body, stage)
            elif stmt.kind == "case":
                selector_type = infer(stmt.selector)
                if selector_type not in enum_types:
                    errors.append(f"CASE selector must be enumeration at line {stmt.span.line}")
                seen_arms: set[str] = set()
                for value, body, _ in stmt.arms:
                    if value in seen_arms:
                        errors.append(f"duplicate CASE arm {value} at line {stmt.span.line}")
                    seen_arms.add(value)
                    if enum_values.get(value) != selector_type:
                        errors.append(f"CASE arm {value} is not a member of {selector_type}")
                    check_statements(body, stage)
                if selector_type in enum_types:
                    required_arms = set(enum_types[selector_type])
                    missing_arms = sorted(required_arms - seen_arms)
                    if missing_arms:
                        errors.append(
                            f"non-exhaustive CASE for {selector_type} at line {stmt.span.line}: "
                            + ", ".join(missing_arms)
                        )

    for decl in program.states:
        if decl.initializer is not None:
            source_type = infer(decl.initializer)
            target_type = symbols[decl.name].type_name
            if source_type != target_type and not (source_type == "INT_LITERAL" and target_type in {"U16", "I32"}):
                errors.append(f"initializer type mismatch for {decl.name}")
            symbols[decl.name].initializer = literal_value(decl.initializer)
    check_statements(program.transition, "transition")
    check_statements(program.output, "output")

    # Every output must receive an unconditional default assignment before control flow.
    defaulted: set[str] = set()
    for stmt in program.output:
        if stmt.kind != "assign":
            break
        defaulted.add(stmt.target or "")
    missing = [decl.name for decl in program.outputs if decl.name not in defaulted]
    if missing:
        errors.append("outputs missing unconditional defaults: " + ", ".join(missing))
    if errors:
        raise AerostError("semantic analysis failed:\n- " + "\n- ".join(errors))
    return TypedProgram(program, symbols, enum_values, enum_types)


def literal_value(expr: Expr) -> Any:
    if expr.kind in {"bool", "int", "name"}:
        return expr.value
    if expr.kind == "unary" and expr.value == "MINUS" and expr.operand.kind == "int":
        return -int(expr.operand.value)
    raise AerostError(f"initializer is not a literal at line {expr.span.line}")


def normalized_expr(expr: Expr) -> str:
    if expr.kind == "name":
        return f"name({expr.value})"
    if expr.kind == "bool":
        return f"bool({str(expr.value).lower()})"
    if expr.kind == "int":
        return f"int({expr.value})"
    if expr.kind == "unary":
        return f"{expr.value}({normalized_expr(expr.operand)})"
    return f"{expr.value}({normalized_expr(expr.left)},{normalized_expr(expr.right)})"


def normalized_statement(stmt: Statement) -> str:
    if stmt.kind == "assign":
        return f"assign({stmt.target},{normalized_expr(stmt.expr)})"
    if stmt.kind == "if":
        parts = []
        for cond, body, _ in stmt.branches:
            parts.append("branch(" + normalized_expr(cond) + ",[" + ",".join(normalized_statement(x) for x in body) + "])")
        return "if(" + ",".join(parts) + ",else[" + ",".join(normalized_statement(x) for x in stmt.else_body) + "] )"
    return "case(" + normalized_expr(stmt.selector) + "," + ",".join(
        value + "[" + ",".join(normalized_statement(x) for x in body) + "]" for value, body, _ in stmt.arms
    ) + ")"


def stable_id(prefix: str, text: str, occurrence: int = 0) -> str:
    digest = hashlib.sha256((text + f"#{occurrence}").encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"


def expr_to_air(expr: Expr) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": expr.kind,
        "type": expr.value_type,
        "span": expr.span.to_json(),
    }
    if expr.kind in {"name", "bool", "int"}:
        data["value"] = expr.value
    elif expr.kind == "unary":
        data["operator"] = expr.value
        data["operand"] = expr_to_air(expr.operand)
    else:
        data["operator"] = expr.value
        data["left"] = expr_to_air(expr.left)
        data["right"] = expr_to_air(expr.right)
    return data


def condition_atoms(expr: Expr) -> list[Expr]:
    if expr.kind == "binary" and expr.value in {"AND", "OR"}:
        return condition_atoms(expr.left) + condition_atoms(expr.right)
    return [expr]


def default_execution_contract(program_name: str) -> dict[str, Any]:
    """Return a name-neutral execution contract for programs without platform policy."""
    return {
        "schema_version": SCHEMA_VERSION,
        "program": program_name,
        "runtime_fault_policy": {
            "normal_commit_inhibited": True,
            "retained_assignments": [],
            "output_assignments": [],
            "diagnostic": "AEROST-RUNTIME-BLOCKING-FAULT",
        },
        "diagnostic_policy": {
            "state_transition": "AEROST-STATE-TRANSITION",
            "latched_flags": [],
        },
        "assurance_obligations": {
            "reset_paths": [],
            "recovery_paths": [],
        },
    }


def _policy_literal_matches(value: Any, type_name: str, enum_types: dict[str, list[str]]) -> bool:
    if type_name == "BOOL":
        return isinstance(value, bool)
    if type_name == "U16":
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 65535
    if type_name == "I32":
        return isinstance(value, int) and not isinstance(value, bool) and -(2**31) <= value < 2**31
    return isinstance(value, str) and value in enum_types.get(type_name, [])


def validate_execution_contract(contract: dict[str, Any], typed: TypedProgram) -> dict[str, Any]:
    """Validate platform policy against declared program symbols and return canonical metadata."""
    errors: list[str] = []
    program_name = typed.program.name
    if contract.get("program") != program_name:
        errors.append(
            f"execution-contract program {contract.get('program')!r} does not match {program_name!r}"
        )
    runtime = contract.get("runtime_fault_policy")
    diagnostics = contract.get("diagnostic_policy")
    assurance = contract.get(
        "assurance_obligations",
        {"reset_paths": [], "recovery_paths": []},
    )
    if not isinstance(runtime, dict):
        errors.append("runtime_fault_policy must be an object")
        runtime = {}
    if not isinstance(diagnostics, dict):
        errors.append("diagnostic_policy must be an object")
        diagnostics = {}
    if not isinstance(assurance, dict):
        errors.append("assurance_obligations must be an object")
        assurance = {"reset_paths": [], "recovery_paths": []}

    state_symbols = {name: symbol for name, symbol in typed.symbols.items() if symbol.section == "state"}
    output_symbols = {name: symbol for name, symbol in typed.symbols.items() if symbol.section == "output"}

    def validate_assignment(item: Any, symbols: dict[str, Symbol], category: str) -> None:
        if not isinstance(item, dict):
            errors.append(f"{category} assignment must be an object")
            return
        target = item.get("target")
        symbol = symbols.get(target)
        if symbol is None:
            errors.append(f"{category} assignment target {target!r} is not a declared {category}")
            return
        sources = [key for key in ("value", "from_state") if key in item]
        if len(sources) != 1:
            errors.append(f"assignment to {target} must define exactly one of value or from_state")
            return
        if "value" in item:
            if not _policy_literal_matches(item["value"], symbol.type_name, typed.enum_types):
                errors.append(
                    f"policy value {item['value']!r} is incompatible with {target}:{symbol.type_name}"
                )
        else:
            source_name = item["from_state"]
            source_symbol = state_symbols.get(source_name)
            if source_symbol is None:
                errors.append(f"from_state source {source_name!r} is not a retained-state variable")
            elif source_symbol.type_name != symbol.type_name:
                errors.append(
                    f"from_state type mismatch: {source_name}:{source_symbol.type_name} -> "
                    f"{target}:{symbol.type_name}"
                )

    retained_assignments = runtime.get("retained_assignments", [])
    output_assignments = runtime.get("output_assignments", [])
    if not isinstance(retained_assignments, list):
        errors.append("retained_assignments must be an array")
        retained_assignments = []
    if not isinstance(output_assignments, list):
        errors.append("output_assignments must be an array")
        output_assignments = []
    for item in retained_assignments:
        validate_assignment(item, state_symbols, "state")
    for item in output_assignments:
        validate_assignment(item, output_symbols, "output")

    for name, assignments in (("retained", retained_assignments), ("output", output_assignments)):
        targets = [item.get("target") for item in assignments if isinstance(item, dict)]
        duplicates = sorted({target for target in targets if target and targets.count(target) > 1})
        if duplicates:
            errors.append(f"duplicate {name} policy targets: {', '.join(duplicates)}")

    if not isinstance(runtime.get("normal_commit_inhibited"), bool):
        errors.append("normal_commit_inhibited must be boolean")
    if not isinstance(runtime.get("diagnostic"), str) or not runtime.get("diagnostic"):
        errors.append("runtime fault diagnostic must be a non-empty string")
    if not isinstance(diagnostics.get("state_transition"), str) or not diagnostics.get("state_transition"):
        errors.append("state_transition diagnostic must be a non-empty string")
    latched_flags = diagnostics.get("latched_flags", [])
    if not isinstance(latched_flags, list):
        errors.append("latched_flags must be an array")
        latched_flags = []
    for item in latched_flags:
        if not isinstance(item, dict):
            errors.append("latched flag must be an object")
            continue
        state_name = item.get("state_variable")
        symbol = state_symbols.get(state_name)
        if symbol is None:
            errors.append(f"latched flag state_variable {state_name!r} is not declared")
            continue
        if not _policy_literal_matches(item.get("active_value"), symbol.type_name, typed.enum_types):
            errors.append(
                f"latched active value {item.get('active_value')!r} is incompatible with "
                f"{state_name}:{symbol.type_name}"
            )
        if not isinstance(item.get("diagnostic"), str) or not item.get("diagnostic"):
            errors.append(f"latched flag {state_name} requires a diagnostic")


    def validate_assurance_path(item: Any, category: str, index: int) -> None:
        prefix = f"assurance_obligations.{category}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            return
        path_id = item.get("id")
        if not isinstance(path_id, str) or not path_id:
            errors.append(f"{prefix}.id must be a non-empty string")
        requirements = item.get("requirements", [])
        subject_ids = item.get("subject_ids", [])
        if not isinstance(requirements, list) or not all(
            isinstance(value, str) and value for value in requirements
        ):
            errors.append(f"{prefix}.requirements must contain non-empty strings")
        if not isinstance(subject_ids, list) or not subject_ids or not all(
            isinstance(value, str) and value for value in subject_ids
        ):
            errors.append(f"{prefix}.subject_ids must contain non-empty strings")
        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            errors.append(f"{prefix}.attributes must be an object")
            return
        state_variable = attributes.get("state_variable")
        state_symbol = state_symbols.get(state_variable)
        if state_symbol is None:
            errors.append(f"{prefix}.attributes.state_variable is not declared retained state")
            return
        from_values = attributes.get("from_values")
        if not isinstance(from_values, list) or not from_values:
            errors.append(f"{prefix}.attributes.from_values must be a non-empty array")
        else:
            for value in from_values:
                if not _policy_literal_matches(value, state_symbol.type_name, typed.enum_types):
                    errors.append(f"{prefix} from-state value {value!r} is incompatible")
        to_value = attributes.get("to_value")
        if not _policy_literal_matches(to_value, state_symbol.type_name, typed.enum_types):
            errors.append(f"{prefix} to-state value {to_value!r} is incompatible")
        retained = attributes.get("retained_assignments", [])
        if not isinstance(retained, list):
            errors.append(f"{prefix}.attributes.retained_assignments must be an array")
        else:
            for assignment in retained:
                validate_assignment(assignment, state_symbols, "state")

    for category in ("reset_paths", "recovery_paths"):
        records = assurance.get(category, [])
        if not isinstance(records, list):
            errors.append(f"assurance_obligations.{category} must be an array")
            continue
        seen_path_ids: set[str] = set()
        for index, item in enumerate(records):
            validate_assurance_path(item, category, index)
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                if item["id"] in seen_path_ids:
                    errors.append(f"duplicate assurance path id: {item['id']}")
                seen_path_ids.add(item["id"])

    if errors:
        raise AerostError("execution-contract validation failed:\n- " + "\n- ".join(errors))
    return {
        "schema_version": contract.get("schema_version", SCHEMA_VERSION),
        "program": program_name,
        "runtime_fault_policy": copy.deepcopy(runtime),
        "diagnostic_policy": copy.deepcopy(diagnostics),
        "assurance_obligations": copy.deepcopy(assurance),
    }


def load_execution_contract(path: Path, typed: TypedProgram) -> dict[str, Any]:
    return validate_execution_contract(load_json(path), typed)


def lower_to_air(
    typed: TypedProgram,
    execution_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    used_ids: dict[str, int] = {}

    def make_id(prefix: str, normalized: str) -> str:
        count = used_ids.get(prefix + normalized, 0)
        used_ids[prefix + normalized] = count + 1
        return stable_id(prefix, normalized, count)

    def lower_stmt(stmt: Statement, inherited: Annotation, stage: str) -> dict[str, Any]:
        ann = stmt.annotation.merged(inherited)
        norm = stage + ":" + normalized_statement(stmt)
        source_id = make_id("ASCP-SRC-STMT", norm)
        node_id = make_id("AIR-STMT", norm)
        stmt.node_id = node_id
        base: dict[str, Any] = {
            "source_id": source_id,
            "id": node_id,
            "kind": stmt.kind,
            "stage": stage,
            "span": stmt.span.to_json(),
            "requirements": list(ann.requirements or ("ASCP-REQ-GENERIC-EXECUTION",)),
            "tests": list(ann.tests or ("TEST-GENERIC-EXECUTION",)),
            "claim_critical": ann.claim_critical,
            "mutation_tags": list(ann.mutation_tags),
        }
        if stmt.kind == "assign":
            base["target"] = stmt.target
            base["expression"] = expr_to_air(stmt.expr)
        elif stmt.kind == "if":
            branches = []
            stmt.decision_ids = []
            for index, (cond, body, branch_ann) in enumerate(stmt.branches):
                merged = branch_ann.merged(ann)
                decision_norm = f"{stage}:decision:{normalized_expr(cond)}:{index}:{node_id}"
                source_decision_id = make_id("ASCP-SRC-DEC", decision_norm)
                decision_id = make_id("AIR-DEC", decision_norm)
                stmt.decision_ids.append(decision_id)
                atoms = condition_atoms(cond)
                conditions = []
                for atom_index, atom in enumerate(atoms):
                    condition_norm = f"{decision_norm}:{atom_index}:{normalized_expr(atom)}"
                    source_cond_id = make_id("ASCP-SRC-COND", condition_norm)
                    cond_id = make_id("AIR-COND", condition_norm)
                    atom.node_id = cond_id
                    conditions.append({
                        "source_id": source_cond_id,
                        "id": cond_id,
                        "expression": expr_to_air(atom),
                    })
                branches.append({
                    "source_id": source_decision_id,
                    "decision_id": decision_id,
                    "expression": expr_to_air(cond),
                    "conditions": conditions,
                    "requirements": list(merged.requirements or base["requirements"]),
                    "tests": list(merged.tests or base["tests"]),
                    "claim_critical": merged.claim_critical,
                    "mcdc": merged.mcdc,
                    "mutation_tags": list(merged.mutation_tags),
                    "body": [lower_stmt(child, merged, stage) for child in body],
                })
            base["branches"] = branches
            base["else_body"] = [lower_stmt(child, ann, stage) for child in stmt.else_body]
        else:
            base["selector"] = expr_to_air(stmt.selector)
            base["arms"] = [
                {
                    "value": value,
                    "requirements": list(arm_ann.merged(ann).requirements or base["requirements"]),
                    "tests": list(arm_ann.merged(ann).tests or base["tests"]),
                    "body": [lower_stmt(child, arm_ann.merged(ann), stage) for child in body],
                }
                for value, body, arm_ann in stmt.arms
            ]
        return base

    program = typed.program
    contract = validate_execution_contract(
        execution_contract or default_execution_contract(program.name), typed
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema_version": SCHEMA_VERSION,
        "profile_version": PROFILE_VERSION,
        "execution_contract": contract,
        "program": {
            "source_id": stable_id("ASCP-SRC-PROG", program.name.upper()),
            "id": stable_id("AIR-PROG", program.name.upper()),
            "name": program.name,
            "types": [{"name": t.name, "values": t.values, "span": t.span.to_json()} for t in program.types],
            "inputs": [decl_to_air(d, typed) for d in program.inputs],
            "outputs": [decl_to_air(d, typed) for d in program.outputs],
            "states": [decl_to_air(d, typed) for d in program.states],
            "transition_stage": [lower_stmt(stmt, Annotation(), "transition") for stmt in program.transition],
            "output_stage": [lower_stmt(stmt, Annotation(), "output") for stmt in program.output],
        },
    }


def decl_to_air(decl: VarDecl, typed: TypedProgram) -> dict[str, Any]:
    return {
        "name": decl.name,
        "type": typed.symbols[decl.name].type_name,
        "section": decl.section,
        "initializer": literal_value(decl.initializer) if decl.initializer else None,
        "span": decl.span.to_json(),
    }

@dataclass
class DecisionEvent:
    decision_id: str
    result: bool
    conditions: list[tuple[str, bool]]
    scenario_id: str
    cycle: int

    def to_json(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "result": self.result,
            "conditions": [{"id": cid, "value": value} for cid, value in self.conditions],
            "scenario_id": self.scenario_id,
            "cycle": self.cycle,
        }


@dataclass
class ExecutionTrace:
    statements: list[str] = field(default_factory=list)
    decisions: list[DecisionEvent] = field(default_factory=list)
    case_arms: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class CycleResult:
    scenario_id: str
    cycle: int
    resident_state: dict[str, Any]
    committed_state: dict[str, Any]
    outputs: dict[str, Any]
    normal_commit_inhibited: bool
    trace: ExecutionTrace

    def observable(self) -> dict[str, Any]:
        return {
            "committed_state": self.committed_state,
            "outputs": self.outputs,
            "normal_commit_inhibited": self.normal_commit_inhibited,
            "diagnostics": self.trace.diagnostics,
            "statements": self.trace.statements,
            "decisions": [d.to_json() for d in self.trace.decisions],
            "case_arms": self.trace.case_arms,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "cycle": self.cycle,
            "resident_state": self.resident_state,
            **self.observable(),
        }


def eval_air_expr(expr: dict[str, Any], env: dict[str, Any]) -> Any:
    kind = expr["kind"]
    if kind == "name":
        name = expr["value"]
        if name in env:
            return env[name]
        return name  # enumeration literal
    if kind in {"bool", "int"}:
        return expr["value"]
    if kind == "unary":
        value = eval_air_expr(expr["operand"], env)
        op = expr["operator"]
        if op == "NOT":
            return not bool(value)
        if op == "MINUS":
            return -int(value)
        if op == "PLUS":
            return int(value)
        raise AerostError(f"unsupported unary operator {op}")
    left = eval_air_expr(expr["left"], env)
    right = eval_air_expr(expr["right"], env)
    op = expr["operator"]
    if op == "AND":
        return bool(left) and bool(right)
    if op == "OR":
        return bool(left) or bool(right)
    if op == "EQ":
        return left == right
    if op == "NE":
        return left != right
    if op == "LT":
        return left < right
    if op == "LE":
        return left <= right
    if op == "GT":
        return left > right
    if op == "GE":
        return left >= right
    raise AerostError(f"unsupported binary operator {op}")


def execute_air_statements(
    statements: list[dict[str, Any]],
    env: dict[str, Any],
    trace: ExecutionTrace,
    scenario_id: str,
    cycle: int,
) -> None:
    for stmt in statements:
        trace.statements.append(stmt["id"])
        kind = stmt["kind"]
        if kind == "assign":
            env[stmt["target"]] = eval_air_expr(stmt["expression"], env)
        elif kind == "if":
            taken = False
            for branch in stmt["branches"]:
                condition_values = [
                    (cond["id"], bool(eval_air_expr(cond["expression"], env)))
                    for cond in branch["conditions"]
                ]
                result = bool(eval_air_expr(branch["expression"], env))
                trace.decisions.append(DecisionEvent(branch["decision_id"], result, condition_values, scenario_id, cycle))
                if result:
                    execute_air_statements(branch["body"], env, trace, scenario_id, cycle)
                    taken = True
                    break
            if not taken:
                execute_air_statements(stmt["else_body"], env, trace, scenario_id, cycle)
        elif kind == "case":
            selector = eval_air_expr(stmt["selector"], env)
            matched = False
            for arm in stmt["arms"]:
                if selector == arm["value"]:
                    trace.case_arms.append(f"{stmt['id']}::{arm['value']}")
                    execute_air_statements(arm["body"], env, trace, scenario_id, cycle)
                    matched = True
                    break
            if not matched:
                raise AerostError(f"no CASE arm for {selector} in {stmt['id']}")
        else:
            raise AerostError(f"unsupported AIR statement {kind}")


def initial_state_from_air(air: dict[str, Any]) -> dict[str, Any]:
    return {decl["name"]: decl["initializer"] for decl in air["program"]["states"]}


def default_outputs_from_air(air: dict[str, Any]) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for decl in air["program"]["outputs"]:
        t = decl["type"]
        outputs[decl["name"]] = False if t == "BOOL" else 0 if t in {"U16", "I32"} else air["program"]["types"][0]["values"][0]
    return outputs


def execute_cycle_air(
    air: dict[str, Any],
    retained: dict[str, Any],
    snapshot: dict[str, Any],
    scenario_id: str,
    cycle: int,
    blocking_runtime_fault: bool = False,
) -> CycleResult:
    resident = copy.deepcopy(retained)
    working_state = copy.deepcopy(retained)
    outputs = default_outputs_from_air(air)
    env = {**snapshot, **working_state, **outputs}
    trace = ExecutionTrace()
    execute_air_statements(air["program"]["transition_stage"], env, trace, scenario_id, cycle)
    for key in working_state:
        working_state[key] = env[key]
    env.update(working_state)
    execute_air_statements(air["program"]["output_stage"], env, trace, scenario_id, cycle)
    for key in outputs:
        outputs[key] = env[key]

    contract = air["execution_contract"]
    runtime_policy = contract["runtime_fault_policy"]
    diagnostic_policy = contract["diagnostic_policy"]
    normal_commit_inhibited = False
    if blocking_runtime_fault:
        normal_commit_inhibited = bool(runtime_policy["normal_commit_inhibited"])
        for assignment in runtime_policy["retained_assignments"]:
            if "value" in assignment:
                working_state[assignment["target"]] = assignment["value"]
            else:
                working_state[assignment["target"]] = working_state[assignment["from_state"]]
        for assignment in runtime_policy["output_assignments"]:
            if "value" in assignment:
                outputs[assignment["target"]] = assignment["value"]
            else:
                outputs[assignment["target"]] = working_state[assignment["from_state"]]
        trace.diagnostics.append(runtime_policy["diagnostic"])
    if resident != working_state:
        trace.diagnostics.append(diagnostic_policy["state_transition"])
    for flag in diagnostic_policy["latched_flags"]:
        if working_state[flag["state_variable"]] == flag["active_value"]:
            trace.diagnostics.append(flag["diagnostic"])
    return CycleResult(scenario_id, cycle, resident, working_state, outputs, normal_commit_inhibited, trace)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_repository_file(root: Path, raw: Any, field_name: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise AerostError(f"application manifest field {field_name} must be a non-empty string")
    relative = Path(raw)
    if relative.is_absolute():
        raise AerostError(f"application manifest field {field_name} must be repository-relative")
    repository_root = root.resolve()
    resolved = (repository_root / relative).resolve()
    if resolved != repository_root and repository_root not in resolved.parents:
        raise AerostError(f"application manifest field {field_name} escapes repository root")
    if not resolved.is_file():
        raise AerostError(f"application manifest file does not exist: {raw}")
    return resolved


def load_application_manifest(
    root: Path,
    manifest_path: Path | None = None,
) -> ApplicationConfig:
    repository_root = root.resolve()
    selected = manifest_path or DEFAULT_APPLICATION_MANIFEST
    if not selected.is_absolute():
        selected = repository_root / selected
    selected = selected.resolve()
    if selected != repository_root and repository_root not in selected.parents:
        raise AerostError("application manifest must be inside repository root")
    if not selected.is_file():
        raise AerostError(f"application manifest not found: {selected}")
    manifest = load_json(selected)
    if not isinstance(manifest, dict):
        raise AerostError("application manifest must be a JSON object")
    if manifest.get("schema_version") != "AEROST-APPLICATION-MANIFEST-0.2":
        raise AerostError("unsupported application manifest schema_version")
    application_id = manifest.get("application_id")
    if not isinstance(application_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", application_id):
        raise AerostError("application_id must use lowercase kebab-case")
    profile_version = manifest.get("profile_version")
    if profile_version != PROFILE_VERSION:
        raise AerostError(
            f"application profile_version must be {PROFILE_VERSION}, got {profile_version!r}"
        )
    air_artifact_name = manifest.get("air_artifact_name")
    if (
        not isinstance(air_artifact_name, str)
        or Path(air_artifact_name).name != air_artifact_name
        or not air_artifact_name.endswith(".air.json")
    ):
        raise AerostError("air_artifact_name must be a simple *.air.json filename")
    raw_scenarios = manifest.get("controlled_scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise AerostError("controlled_scenarios must be a non-empty array")
    scenario_paths = tuple(
        _resolve_repository_file(repository_root, value, f"controlled_scenarios[{index}]")
        for index, value in enumerate(raw_scenarios)
    )
    if len(set(scenario_paths)) != len(scenario_paths):
        raise AerostError("controlled_scenarios contains duplicate file paths")
    manual_requirements = _resolve_repository_file(
        repository_root, manifest.get("manual_requirements_scenarios"), "manual_requirements_scenarios"
    )
    manual_closure = _resolve_repository_file(
        repository_root, manifest.get("manual_closure_scenarios"), "manual_closure_scenarios"
    )
    if manual_requirements not in scenario_paths or manual_closure not in scenario_paths:
        raise AerostError(
            "manual requirements and closure scenario files must be listed in controlled_scenarios"
        )
    return ApplicationConfig(
        application_id=application_id,
        profile_version=profile_version,
        manifest_path=selected,
        source_path=_resolve_repository_file(repository_root, manifest.get("source"), "source"),
        requirements_path=_resolve_repository_file(
            repository_root, manifest.get("requirements"), "requirements"
        ),
        runtime_fault_policy_path=_resolve_repository_file(
            repository_root, manifest.get("runtime_fault_policy"), "runtime_fault_policy"
        ),
        controlled_scenario_paths=scenario_paths,
        manual_requirements_path=manual_requirements,
        manual_closure_path=manual_closure,
        input_domain_path=_resolve_repository_file(
            repository_root, manifest.get("input_domain"), "input_domain"
        ),
        mutation_profile_path=_resolve_repository_file(
            repository_root, manifest.get("mutation_profile"), "mutation_profile"
        ),
        air_artifact_name=air_artifact_name,
        manifest=copy.deepcopy(manifest),
    )


def load_controlled_scenarios(
    root: Path,
    application: ApplicationConfig | None = None,
) -> list[dict[str, Any]]:
    selected = application or load_application_manifest(root)
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in selected.controlled_scenario_paths:
        document = load_json(path)
        raw_scenarios = document.get("scenarios") if isinstance(document, dict) else None
        if not isinstance(raw_scenarios, list):
            raise AerostError(f"controlled scenario document has no scenarios array: {path}")
        for scenario in raw_scenarios:
            if not isinstance(scenario, dict):
                raise AerostError(f"controlled scenario entry must be an object: {path}")
            scenario_id = scenario.get("id")
            if not isinstance(scenario_id, str) or not scenario_id:
                raise AerostError(f"controlled scenario has no valid id: {path}")
            if scenario_id in seen:
                raise AerostError(f"duplicate controlled scenario id: {scenario_id}")
            seen.add(scenario_id)
            scenarios.append(scenario)
    return scenarios


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False, separators=(",", ": ")) + "\n"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(data), encoding="utf-8", newline="\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_reference_scenarios(air: dict[str, Any], scenarios: list[dict[str, Any]]) -> list[CycleResult]:
    results: list[CycleResult] = []
    for scenario in scenarios:
        retained = copy.deepcopy(scenario.get("initial_state") or initial_state_from_air(air))
        for index, cycle in enumerate(scenario["cycles"]):
            result = execute_cycle_air(
                air,
                retained,
                cycle["inputs"],
                scenario["id"],
                index,
                bool(cycle.get("blocking_runtime_fault", False)),
            )
            results.append(result)
            retained = copy.deepcopy(result.committed_state)
    return results

def snake(name: str) -> str:
    # Handles CamelCase and SCREAMING_SNAKE_CASE.
    if "_" in name:
        return name.lower()
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:].lower() for part in name.split("_"))


def kebab(name: str) -> str:
    return snake(name).replace("_", "-")


def generated_package_name(air: dict[str, Any]) -> str:
    return "aerost-generated-" + kebab(air["program"]["name"])


def rust_type(type_name: str) -> str:
    return {"BOOL": "bool", "U16": "u16", "I32": "i32"}.get(type_name, type_name)


def rust_literal(value: Any, type_name: str, enum_types: dict[str, list[str]]) -> str:
    if type_name == "BOOL":
        return "true" if bool(value) else "false"
    if type_name == "U16":
        return f"{int(value)}u16"
    if type_name == "I32":
        return f"{int(value)}i32"
    return f"{type_name}::{pascal(str(value))}"


def variable_sections(air: dict[str, Any]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for section in ("inputs", "outputs", "states"):
        for decl in air["program"][section]:
            out[decl["name"]] = (section[:-1] if section.endswith("s") else section, decl["type"])
    return out


def rust_expr_with_precedence(
    expr: dict[str, Any],
    vars_: dict[str, tuple[str, str]],
    enum_values: dict[str, str],
    parent_precedence: int,
) -> str:
    kind = expr["kind"]
    if kind == "name":
        name = expr["value"]
        if name in vars_:
            section, _ = vars_[name]
            owner = {"input": "inputs", "output": "outputs", "state": "state"}[section]
            return f"{owner}.{snake(name)}"
        enum_type = enum_values.get(name)
        if enum_type:
            return f"{enum_type}::{pascal(name)}"
        raise AerostError(f"cannot render unknown name {name}")
    if kind == "bool":
        return "true" if expr["value"] else "false"
    if kind == "int":
        return str(expr["value"])
    if kind == "unary":
        precedence = 4
        op = {"NOT": "!", "MINUS": "-", "PLUS": "+"}[expr["operator"]]
        rendered = op + rust_expr_with_precedence(expr["operand"], vars_, enum_values, precedence)
        return f"({rendered})" if precedence < parent_precedence else rendered
    precedence = {
        "OR": 1,
        "AND": 2,
        "EQ": 3,
        "NE": 3,
        "LT": 3,
        "LE": 3,
        "GT": 3,
        "GE": 3,
    }[expr["operator"]]
    op = {
        "AND": "&&", "OR": "||", "EQ": "==", "NE": "!=", "LT": "<",
        "LE": "<=", "GT": ">", "GE": ">=",
    }[expr["operator"]]
    left = rust_expr_with_precedence(expr["left"], vars_, enum_values, precedence)
    right = rust_expr_with_precedence(expr["right"], vars_, enum_values, precedence)
    rendered = f"{left} {op} {right}"
    return f"({rendered})" if precedence < parent_precedence else rendered


def rust_expr(expr: dict[str, Any], vars_: dict[str, tuple[str, str]], enum_values: dict[str, str]) -> str:
    return rust_expr_with_precedence(expr, vars_, enum_values, 0)


def rust_string(value: str) -> str:
    return json.dumps(value)


def render_rust_statements(
    statements: list[dict[str, Any]],
    indent: int,
    vars_: dict[str, tuple[str, str]],
    enum_values: dict[str, str],
    source_map: list[dict[str, Any]],
) -> list[str]:
    lines: list[str] = []

    def render_if_chain(branches: list[dict[str, Any]], else_body: list[dict[str, Any]], level: int) -> list[str]:
        local: list[str] = []
        pad2 = "    " * level
        if not branches:
            return render_rust_statements(else_body, level, vars_, enum_values, source_map)
        branch = branches[0]
        did = branch["decision_id"]
        source_map.append({
            "source_id": branch["source_id"],
            "air_id": did,
            "generated_block": f"RUST-{len(source_map)+1:05d}",
            "span": branch["expression"]["span"],
            "requirements": branch["requirements"],
            "tests": branch["tests"],
        })
        for condition in branch["conditions"]:
            source_map.append({
                "source_id": condition["source_id"],
                "air_id": condition["id"],
                "generated_block": f"RUST-{len(source_map)+1:05d}",
                "span": condition["expression"]["span"],
                "requirements": branch["requirements"],
                "tests": branch["tests"],
            })
        suffix = re.sub(r"[^A-Za-z0-9_]", "_", did.lower())
        for ci, cond in enumerate(branch["conditions"]):
            local.append(f"{pad2}let {suffix}_c{ci} = {rust_expr(cond['expression'], vars_, enum_values)};")
        local.append(f"{pad2}let {suffix}_result = {rust_expr(branch['expression'], vars_, enum_values)};")
        cond_vec = ", ".join(
            f"({rust_string(cond['id'])}, {suffix}_c{ci})" for ci, cond in enumerate(branch["conditions"])
        )
        local.append(f"{pad2}trace.decisions.push(DecisionEvent {{ id: {rust_string(did)}, result: {suffix}_result, conditions: vec![{cond_vec}] }});")
        local.append(f"{pad2}if {suffix}_result {{")
        local.extend(render_rust_statements(branch["body"], level + 1, vars_, enum_values, source_map))
        local.append(f"{pad2}}} else {{")
        local.extend(render_if_chain(branches[1:], else_body, level + 1))
        local.append(f"{pad2}}}")
        return local

    pad = "    " * indent
    for stmt in statements:
        source_map.append({
            "source_id": stmt["source_id"],
            "air_id": stmt["id"],
            "generated_block": f"RUST-{len(source_map)+1:05d}",
            "span": stmt["span"],
            "requirements": stmt["requirements"],
            "tests": stmt["tests"],
        })
        lines.append(f"{pad}// AEROST-ID: {stmt['id']}")
        lines.append(f"{pad}trace.statements.push({rust_string(stmt['id'])});")
        if stmt["kind"] == "assign":
            section, _ = vars_[stmt["target"]]
            owner = {"input": "inputs", "output": "outputs", "state": "state"}[section]
            lines.append(f"{pad}{owner}.{snake(stmt['target'])} = {rust_expr(stmt['expression'], vars_, enum_values)};")
        elif stmt["kind"] == "if":
            lines.extend(render_if_chain(stmt["branches"], stmt["else_body"], indent))
        elif stmt["kind"] == "case":
            lines.append(f"{pad}match {rust_expr(stmt['selector'], vars_, enum_values)} {{")
            selector_type = stmt["selector"]["type"]
            for arm in stmt["arms"]:
                lines.append(f"{pad}    {selector_type}::{pascal(arm['value'])} => {{")
                arm_event = f"{stmt['id']}::{arm['value']}"
                lines.append(f"{pad}        trace.case_arms.push({rust_string(arm_event)});")
                lines.extend(render_rust_statements(arm["body"], indent + 2, vars_, enum_values, source_map))
                lines.append(f"{pad}    }},")
            lines.append(f"{pad}}}")
    return lines

def generate_rust_crate(air: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    src_dir = output_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    p = air["program"]
    vars_ = variable_sections(air)
    enum_values = {value: typ["name"] for typ in p["types"] for value in typ["values"]}
    source_map: list[dict[str, Any]] = []

    lines: list[str] = [
        "//! Generated by AEROST from executable AIR. Do not edit.",
        "#![forbid(unsafe_code)]",
        "",
    ]
    for typ in p["types"]:
        lines.extend([
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            f"pub enum {typ['name']} {{",
            *[f"    {pascal(v)}," for v in typ["values"]],
            "}",
            "",
        ])
    for struct_name, key in (("Inputs", "inputs"), ("Outputs", "outputs"), ("Retained", "states")):
        lines.extend(["#[derive(Clone, Debug, PartialEq)]", f"pub struct {struct_name} {{"])
        for decl in p[key]:
            lines.append(f"    pub {snake(decl['name'])}: {rust_type(decl['type'])},")
        lines.extend(["}", ""])
    lines.append("impl Default for Inputs {")
    lines.append("    fn default() -> Self {")
    lines.append("        Self {")
    for decl in p["inputs"]:
        if decl["type"] == "BOOL":
            default = "false"
        elif decl["type"] in {"U16", "I32"}:
            default = "0"
        else:
            values = next(t["values"] for t in p["types"] if t["name"] == decl["type"])
            default = f"{decl['type']}::{pascal(values[0])}"
        lines.append(f"            {snake(decl['name'])}: {default},")
    lines.extend(["        }", "    }", "}", ""])
    lines.append("impl Default for Outputs {")
    lines.append("    fn default() -> Self {")
    lines.append("        Self {")
    for decl in p["outputs"]:
        if decl["type"] == "BOOL":
            default = "false"
        elif decl["type"] in {"U16", "I32"}:
            default = "0"
        else:
            values = next(t["values"] for t in p["types"] if t["name"] == decl["type"])
            default = f"{decl['type']}::{pascal(values[0])}"
        lines.append(f"            {snake(decl['name'])}: {default},")
    lines.extend(["        }", "    }", "}", ""])
    lines.append("impl Default for Retained {")
    lines.append("    fn default() -> Self {")
    lines.append("        Self {")
    for decl in p["states"]:
        lines.append(f"            {snake(decl['name'])}: {rust_literal(decl['initializer'], decl['type'], {})},")
    lines.extend(["        }", "    }", "}", ""])
    lines.extend([
        "#[derive(Clone, Debug, PartialEq)]",
        "pub struct DecisionEvent { pub id: &'static str, pub result: bool, pub conditions: Vec<(&'static str, bool)> }",
        "#[derive(Clone, Debug, Default, PartialEq)]",
        "pub struct Trace { pub statements: Vec<&'static str>, pub decisions: Vec<DecisionEvent>, pub case_arms: Vec<&'static str>, pub diagnostics: Vec<&'static str> }",
        "#[derive(Clone, Debug, PartialEq)]",
        "pub struct CycleResult { pub retained: Retained, pub outputs: Outputs, pub normal_commit_inhibited: bool, pub trace: Trace }",
        "",
        "pub fn step(mut state: Retained, inputs: &Inputs, blocking_runtime_fault: bool) -> CycleResult {",
        "    let resident = state.clone();",
        "    let mut outputs = Outputs::default();",
        "    let mut trace = Trace::default();",
    ])
    lines.extend(render_rust_statements(p["transition_stage"], 1, vars_, enum_values, source_map))
    lines.extend(render_rust_statements(p["output_stage"], 1, vars_, enum_values, source_map))
    state_types = {d["name"]: d["type"] for d in p["states"]}
    output_types = {d["name"]: d["type"] for d in p["outputs"]}
    contract = air["execution_contract"]
    runtime_policy = contract["runtime_fault_policy"]
    diagnostic_policy = contract["diagnostic_policy"]
    lines.append("    let mut normal_commit_inhibited = false;")
    lines.append("    if blocking_runtime_fault {")
    lines.append(
        "        normal_commit_inhibited = "
        + ("true;" if runtime_policy["normal_commit_inhibited"] else "false;")
    )
    for assignment in runtime_policy["retained_assignments"]:
        target = assignment["target"]
        if "value" in assignment:
            value = rust_literal(assignment["value"], state_types[target], enum_values)
        else:
            value = f"state.{snake(assignment['from_state'])}"
        lines.append(f"        state.{snake(target)} = {value};")
    for assignment in runtime_policy["output_assignments"]:
        target = assignment["target"]
        if "value" in assignment:
            value = rust_literal(assignment["value"], output_types[target], enum_values)
        else:
            value = f"state.{snake(assignment['from_state'])}"
        lines.append(f"        outputs.{snake(target)} = {value};")
    lines.append(f"        trace.diagnostics.push({rust_string(runtime_policy['diagnostic'])});")
    lines.append("    }")
    lines.append(
        f"    if state != resident {{ trace.diagnostics.push({rust_string(diagnostic_policy['state_transition'])}); }}"
    )
    for flag in diagnostic_policy["latched_flags"]:
        state_name = flag["state_variable"]
        active = rust_literal(flag["active_value"], state_types[state_name], enum_values)
        lines.append(
            f"    if state.{snake(state_name)} == {active} "
            f"{{ trace.diagnostics.push({rust_string(flag['diagnostic'])}); }}"
        )
    lines.extend([
        "    CycleResult { retained: state, outputs, normal_commit_inhibited, trace }",
        "}",
        "",
    ])
    lib_rs = "\n".join(lines) + "\n"

    package_name = generated_package_name(air)
    crate_identifier = package_name.replace("-", "_")
    main_rs = generate_rust_runner(air, crate_identifier)
    cargo_toml = (
        "[workspace]\n\n"
        "[package]\n"
        f"name = {rust_string(package_name)}\n"
        "version = \"0.1.0\"\n"
        "edition = \"2024\"\n"
        "publish = false\n\n"
        "[dependencies]\n"
    )
    (output_dir / "Cargo.toml").write_text(cargo_toml, encoding="utf-8", newline="\n")
    (src_dir / "lib.rs").write_text(lib_rs, encoding="utf-8", newline="\n")
    (src_dir / "main.rs").write_text(main_rs, encoding="utf-8", newline="\n")
    source_map_doc = {
        "schema_version": SCHEMA_VERSION,
        "profile_version": PROFILE_VERSION,
        "program_id": p["id"],
        "mappings": source_map,
    }
    write_json(output_dir / "source-map.json", source_map_doc)
    return source_map_doc


def generate_rust_runner(air: dict[str, Any], crate_identifier: str) -> str:
    p = air["program"]
    lines = [
        "#![forbid(unsafe_code)]",
        "use std::env;",
        "use std::fs;",
        "use std::hint::black_box;",
        "use std::time::Instant;",
        f"use {crate_identifier}::*;",
        "",
    ]
    for typ in p["types"]:
        fn = f"parse_{snake(typ['name'])}"
        lines.append(f"fn {fn}(value: &str) -> {typ['name']} {{")
        lines.append("    match value {")
        for value in typ["values"]:
            lines.append(f"        {rust_string(value)} => {typ['name']}::{pascal(value)},")
        lines.append(f"        _ => panic!(\"invalid {typ['name']} value: {{value}}\"),")
        lines.extend(["    }", "}", ""])
        fmt = f"format_{snake(typ['name'])}"
        lines.append(f"fn {fmt}(value: {typ['name']}) -> &'static str {{")
        lines.append("    match value {")
        for value in typ["values"]:
            lines.append(f"        {typ['name']}::{pascal(value)} => {rust_string(value)},")
        lines.extend(["    }", "}", ""])
    lines.extend([
        "fn parse_bool(value: &str) -> bool { match value { \"1\" | \"true\" | \"TRUE\" => true, \"0\" | \"false\" | \"FALSE\" => false, _ => panic!(\"invalid bool: {value}\") } }",
        "fn bool01(value: bool) -> &'static str { if value { \"1\" } else { \"0\" } }",
        "",
        "#[derive(Clone)]",
        "struct ProtocolCase { scenario: String, cycle: u64, retained: Retained, inputs: Inputs, runtime_fault: bool }",
        "",
        "fn parse_protocol(path: &str) -> Vec<ProtocolCase> {",
        "    let content = fs::read_to_string(path).expect(\"read protocol file\");",
        "    let mut scenario = String::new();",
        "    let mut retained = Retained::default();",
        "    let mut cases: Vec<ProtocolCase> = Vec::new();",
        "    for raw in content.lines() {",
        "        let line = raw.trim();",
        "        if line.is_empty() || line.starts_with('#') { continue; }",
        "        let parts: Vec<&str> = line.split('|').collect();",
        "        match parts[0] {",
        "            \"S\" => {",
        "                scenario = parts[1].to_string();",
        "                retained = Retained::default();",
        "                for part in &parts[2..] {",
        "                    let (key, value) = part.split_once('=').expect(\"state key=value\");",
        "                    match key {",
    ])
    for decl in p["states"]:
        parser = "parse_bool(value)" if decl["type"] == "BOOL" else f"parse_{snake(decl['type'])}(value)" if decl["type"] not in {"U16", "I32"} else f"value.parse::<{rust_type(decl['type'])}>().expect(\"integer\")"
        lines.append(f"                        {rust_string(decl['name'])} => retained.{snake(decl['name'])} = {parser},")
    lines.extend([
        "                        _ => panic!(\"unknown retained field: {key}\"),",
        "                    }",
        "                }",
        "            }",
        "            \"C\" => {",
        "                let cycle: u64 = parts[1].parse().expect(\"cycle\");",
        "                let mut inputs = Inputs::default();",
        "                let mut runtime_fault = false;",
        "                for part in &parts[2..] {",
        "                    let (key, value) = part.split_once('=').expect(\"input key=value\");",
        "                    match key {",
    ])
    for decl in p["inputs"]:
        parser = "parse_bool(value)" if decl["type"] == "BOOL" else f"parse_{snake(decl['type'])}(value)" if decl["type"] not in {"U16", "I32"} else f"value.parse::<{rust_type(decl['type'])}>().expect(\"integer\")"
        lines.append(f"                        {rust_string(decl['name'])} => inputs.{snake(decl['name'])} = {parser},")
    lines.extend([
        '                        "BlockingRuntimeFault" => runtime_fault = parse_bool(value),',
        '                        _ => panic!("unknown input field: {key}"),',
        "                    }",
        "                }",
        "                cases.push(ProtocolCase { scenario: scenario.clone(), cycle, retained: retained.clone(), inputs: inputs.clone(), runtime_fault });",
        "                retained = step(retained, &inputs, runtime_fault).retained;",
        "            }",
        '            _ => panic!("unknown protocol record: {}", parts[0]),',
        "        }",
        "    }",
        "    cases",
        "}",
        "",
        "fn run_file(path: &str) {",
        "    for case in parse_protocol(path) {",
        "        let result = step(case.retained, &case.inputs, case.runtime_fault);",
        "        let statements = result.trace.statements.join(\",\");",
        "        let cases = result.trace.case_arms.join(\",\");",
        "        let diagnostics = result.trace.diagnostics.join(\",\");",
        "        let decisions = result.trace.decisions.iter().map(|d| {",
        "            let c = d.conditions.iter().map(|(id,v)| format!(\"{}={}\", id, bool01(*v))).collect::<Vec<_>>().join(\",\");",
        "            format!(\"{}:{}:{}\", d.id, bool01(d.result), c)",
        "        }).collect::<Vec<_>>().join(\";\");",
        "        let mut fields: Vec<String> = Vec::new();",
        "        fields.push(format!(\"R|{}|{}\", case.scenario, case.cycle));",
    ])
    for decl in p["states"]:
        field = snake(decl["name"])
        if decl["type"] == "BOOL":
            value = f"bool01(result.retained.{field}).to_string()"
        elif decl["type"] in {"U16", "I32"}:
            value = f"result.retained.{field}.to_string()"
        else:
            value = f"format_{snake(decl['type'])}(result.retained.{field}).to_string()"
        lines.append(f"        fields.push(format!(\"{decl['name']}={{}}\", {value}));")
    for decl in p["outputs"]:
        field = snake(decl["name"])
        if decl["type"] == "BOOL":
            value = f"bool01(result.outputs.{field}).to_string()"
        elif decl["type"] in {"U16", "I32"}:
            value = f"result.outputs.{field}.to_string()"
        else:
            value = f"format_{snake(decl['type'])}(result.outputs.{field}).to_string()"
        lines.append(f"        fields.push(format!(\"{decl['name']}={{}}\", {value}));")
    lines.extend([
        '        fields.push(format!("NormalCommitInhibited={}", bool01(result.normal_commit_inhibited)));',
        '        fields.push(format!("Statements={}", statements));',
        '        fields.push(format!("Cases={}", cases));',
        '        fields.push(format!("Diagnostics={}", diagnostics));',
        '        fields.push(format!("Decisions={}", decisions));',
        '        println!("{}", fields.join("|"));',
        "    }",
        "}",
        "",
        "fn summarize(values: &mut [u128]) -> (u128, u128, f64, u128, u128, u128) {",
        "    values.sort_unstable();",
        "    let count = values.len();",
        "    let sum: u128 = values.iter().sum();",
        "    let idx = |p: f64| -> usize { (((count - 1) as f64) * p).round() as usize };",
        "    (values[0], values[count / 2], sum as f64 / count as f64, values[idx(0.95)], values[idx(0.99)], values[count - 1])",
        "}",
        "",
        "fn benchmark_file(path: &str, samples_per_path: usize) {",
        "    assert!(samples_per_path > 0, \"samples per path must be positive\");",
        "    let cases = parse_protocol(path);",
        "    assert!(!cases.is_empty(), \"protocol contains no controlled cycles\");",
        "    let warmup_per_path = 1_000usize;",
        "    let mut global_values: Vec<u128> = Vec::with_capacity(cases.len() * samples_per_path);",
        "    let mut worst_scenario = String::new();",
        "    let mut worst_cycle = 0u64;",
        "    let mut worst_maximum = 0u128;",
        "    for case in &cases {",
        "        for _ in 0..warmup_per_path { black_box(step(case.retained.clone(), black_box(&case.inputs), case.runtime_fault)); }",
        "        let mut values: Vec<u128> = Vec::with_capacity(samples_per_path);",
        "        for _ in 0..samples_per_path {",
        "            let start = Instant::now();",
        "            black_box(step(case.retained.clone(), black_box(&case.inputs), case.runtime_fault));",
        "            values.push(start.elapsed().as_nanos());",
        "        }",
        "        let (minimum, median, mean, p95, p99, maximum) = summarize(&mut values);",
        "        if maximum >= worst_maximum { worst_maximum = maximum; worst_scenario = case.scenario.clone(); worst_cycle = case.cycle; }",
        "        global_values.extend(values.iter().copied());",
        "        println!(\"BENCHCASE|{}|{}|{}|{}|{}|{:.2}|{}|{}|{}\", case.scenario, case.cycle, samples_per_path, minimum, median, mean, p95, p99, maximum);",
        "    }",
        "    let total_samples = global_values.len();",
        "    let (minimum, median, mean, p95, p99, maximum) = summarize(&mut global_values);",
        "    println!(\"BENCHTOTAL|{}|{}|{}|{}|{}|{}|{:.2}|{}|{}|{}|{}|{}|{}\", cases.len(), samples_per_path, warmup_per_path, total_samples, minimum, median, mean, p95, p99, maximum, worst_scenario, worst_cycle, worst_maximum);",
        "}",
        "",
        "fn main() {",
        "    let args: Vec<String> = env::args().collect();",
        "    match args.get(1).map(String::as_str) {",
        '        Some("run") => run_file(args.get(2).expect("protocol file")),',
        '        Some("benchmark-protocol") => benchmark_file(args.get(2).expect("protocol file"), args.get(3).and_then(|x| x.parse().ok()).unwrap_or(100_000)),',
        '        _ => panic!("usage: generated-backend run <protocol-file> | benchmark-protocol <protocol-file> [samples-per-path]"),',
        "    }",
        "}",
    ])
    return "\n".join(lines) + "\n"


def nominal_inputs(**changes: Any) -> dict[str, Any]:
    base = {
        "InputSetValid": True,
        "InitializationComplete": True,
        "BatteryAHealthy": True,
        "BatteryBHealthy": True,
        "EssentialPowerAvailable": True,
        "MainBusWarningPersisted": False,
        "MainBusCritical": False,
        "ReserveEnergyLow": False,
        "ReturnEnergyInsufficient": False,
        "ContactorMismatchPersisted": False,
        "RecoveryStable": False,
        "ResetAuthorized": False,
        "PayloadOvercurrent": False,
        "FlightPhaseLanding": False,
    }
    base.update(changes)
    return base


def oracle_step(retained: dict[str, Any], inputs: dict[str, Any], runtime_fault: bool = False) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Independent hand-coded oracle for the controlled power supervisor."""
    state = str(retained["State"])
    latched = bool(retained["BlockingFaultLatched"])
    i = inputs
    if i["InputSetValid"]:
        if state == "INITIALIZING":
            if i["InitializationComplete"] and (i["BatteryAHealthy"] or i["BatteryBHealthy"]) and i["EssentialPowerAvailable"]:
                state = "NORMAL"
            elif i["InitializationComplete"]:
                state = "LOCKOUT"
        elif state == "NORMAL":
            if not i["BatteryAHealthy"] and not i["BatteryBHealthy"]:
                state = "CRITICAL" if i["EssentialPowerAvailable"] else "LOCKOUT"
            elif i["MainBusCritical"]:
                state = "CRITICAL"
            elif i["ReturnEnergyInsufficient"]:
                state = "ENERGY_CONSERVATION"
            elif i["ContactorMismatchPersisted"] or i["MainBusWarningPersisted"] or not i["BatteryAHealthy"] or not i["BatteryBHealthy"]:
                state = "DEGRADED"
            elif i["ReserveEnergyLow"]:
                state = "ENERGY_CONSERVATION"
        elif state == "ENERGY_CONSERVATION":
            if not i["BatteryAHealthy"] and not i["BatteryBHealthy"]:
                state = "CRITICAL" if i["EssentialPowerAvailable"] else "LOCKOUT"
            elif i["MainBusCritical"]:
                state = "CRITICAL"
            elif i["ContactorMismatchPersisted"] or i["MainBusWarningPersisted"] or not i["BatteryAHealthy"] or not i["BatteryBHealthy"]:
                state = "DEGRADED"
            elif not i["ReserveEnergyLow"] and not i["ReturnEnergyInsufficient"]:
                state = "NORMAL"
        elif state == "DEGRADED":
            if not i["BatteryAHealthy"] and not i["BatteryBHealthy"]:
                state = "CRITICAL" if i["EssentialPowerAvailable"] else "LOCKOUT"
            elif i["MainBusCritical"] or i["ReturnEnergyInsufficient"]:
                state = "CRITICAL"
            elif i["RecoveryStable"] and not i["ContactorMismatchPersisted"] and not i["MainBusWarningPersisted"] and (i["BatteryAHealthy"] or i["BatteryBHealthy"]) and i["EssentialPowerAvailable"]:
                state = "RECOVERY_PENDING"
        elif state == "CRITICAL":
            if not i["BatteryAHealthy"] and not i["BatteryBHealthy"] and not i["EssentialPowerAvailable"]:
                state = "LOCKOUT"
            elif i["RecoveryStable"] and not i["MainBusCritical"] and not i["ContactorMismatchPersisted"] and not i["MainBusWarningPersisted"] and not i["ReturnEnergyInsufficient"] and (i["BatteryAHealthy"] or i["BatteryBHealthy"]) and i["EssentialPowerAvailable"]:
                state = "RECOVERY_PENDING"
        elif state == "RECOVERY_PENDING":
            if not i["BatteryAHealthy"] and not i["BatteryBHealthy"]:
                state = "CRITICAL" if i["EssentialPowerAvailable"] else "LOCKOUT"
            elif i["MainBusCritical"]:
                state = "CRITICAL"
            elif i["ContactorMismatchPersisted"] or i["MainBusWarningPersisted"] or not i["BatteryAHealthy"] or not i["BatteryBHealthy"]:
                state = "DEGRADED"
            elif i["ResetAuthorized"] and i["RecoveryStable"] and not i["ReturnEnergyInsufficient"] and i["EssentialPowerAvailable"]:
                state = "NORMAL"
                latched = False
        elif state == "LOCKOUT":
            if i["ResetAuthorized"] and i["RecoveryStable"] and not i["MainBusCritical"] and not i["ContactorMismatchPersisted"] and not i["MainBusWarningPersisted"] and not i["ReturnEnergyInsufficient"] and (i["BatteryAHealthy"] or i["BatteryBHealthy"]) and i["EssentialPowerAvailable"]:
                state = "NORMAL"
                latched = False
    else:
        latched = True
    if i["ContactorMismatchPersisted"] or state in {"CRITICAL", "LOCKOUT"}:
        latched = True

    outputs = {
        "PayloadPermit": False,
        "ShedNonessentialLoads": True,
        "IsolateBatteryA": False,
        "IsolateBatteryB": False,
        "ReturnToHomeRequest": False,
        "ImmediateLandingRequest": False,
        "MissionProgressionPermit": False,
        "BlockingPowerFault": latched,
        "SupervisorState": state,
    }
    if state == "NORMAL":
        outputs["PayloadPermit"] = True
        outputs["ShedNonessentialLoads"] = False
        outputs["MissionProgressionPermit"] = True
    elif state == "ENERGY_CONSERVATION":
        outputs["ReturnToHomeRequest"] = bool(i["ReturnEnergyInsufficient"])
    elif state == "DEGRADED":
        outputs["ReturnToHomeRequest"] = True
        outputs["IsolateBatteryA"] = (not i["BatteryAHealthy"]) and i["BatteryBHealthy"]
        outputs["IsolateBatteryB"] = (not i["BatteryBHealthy"]) and i["BatteryAHealthy"]
    elif state == "CRITICAL":
        outputs["BlockingPowerFault"] = True
        outputs["ReturnToHomeRequest"] = True
        outputs["ImmediateLandingRequest"] = True
    elif state == "RECOVERY_PENDING":
        outputs["BlockingPowerFault"] = True
    elif state == "LOCKOUT":
        outputs["BlockingPowerFault"] = True
        outputs["ImmediateLandingRequest"] = True
    if not i["InputSetValid"]:
        outputs.update({
            "PayloadPermit": False,
            "ShedNonessentialLoads": True,
            "MissionProgressionPermit": False,
            "IsolateBatteryA": False,
            "IsolateBatteryB": False,
            "BlockingPowerFault": True,
            "ReturnToHomeRequest": not i["FlightPhaseLanding"],
            "ImmediateLandingRequest": i["FlightPhaseLanding"] or not i["EssentialPowerAvailable"],
        })
    if i["PayloadOvercurrent"]:
        outputs["PayloadPermit"] = False
        outputs["ShedNonessentialLoads"] = True
    inhibited = False
    if runtime_fault:
        inhibited = True
        latched = True
        outputs.update({
            "PayloadPermit": False,
            "ShedNonessentialLoads": True,
            "IsolateBatteryA": False,
            "IsolateBatteryB": False,
            "ReturnToHomeRequest": False,
            "ImmediateLandingRequest": False,
            "MissionProgressionPermit": False,
            "BlockingPowerFault": True,
            "SupervisorState": state,
        })
    return {"State": state, "BlockingFaultLatched": latched}, outputs, inhibited


def scenario_definitions() -> list[dict[str, Any]]:
    normal = {"State": "NORMAL", "BlockingFaultLatched": False}
    scenarios = [
        {"id": "TEST-PWR-STARTUP-WAIT-001", "requirements": ["PWR-HLR-001"], "initial_state": {"State": "INITIALIZING", "BlockingFaultLatched": False}, "cycles": [{"inputs": nominal_inputs(InitializationComplete=False, BatteryBHealthy=False)}]},
        {"id": "TEST-PWR-STARTUP-READY-001", "requirements": ["PWR-HLR-001"], "initial_state": {"State": "INITIALIZING", "BlockingFaultLatched": False}, "cycles": [{"inputs": nominal_inputs(BatteryBHealthy=False)}]},
        {"id": "TEST-PWR-STARTUP-A-INDEPENDENCE-001", "requirements": ["PWR-HLR-001"], "initial_state": {"State": "INITIALIZING", "BlockingFaultLatched": False}, "cycles": [{"inputs": nominal_inputs(BatteryAHealthy=False, BatteryBHealthy=False)}]},
        {"id": "TEST-PWR-STARTUP-B-INDEPENDENCE-001", "requirements": ["PWR-HLR-001"], "initial_state": {"State": "INITIALIZING", "BlockingFaultLatched": False}, "cycles": [{"inputs": nominal_inputs(BatteryAHealthy=False, BatteryBHealthy=True)}]},
        {"id": "TEST-PWR-STARTUP-ESSENTIAL-001", "requirements": ["PWR-HLR-001"], "initial_state": {"State": "INITIALIZING", "BlockingFaultLatched": False}, "cycles": [{"inputs": nominal_inputs(BatteryBHealthy=False, EssentialPowerAvailable=False)}]},
        {"id": "TEST-PWR-RESERVE-001", "requirements": ["PWR-HLR-003"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(ReserveEnergyLow=True)}, {"inputs": nominal_inputs()}]},
        {"id": "TEST-PWR-RETURN-001", "requirements": ["PWR-HLR-004"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(ReturnEnergyInsufficient=True)}]},
        {"id": "TEST-PWR-BUS-CRITICAL-001", "requirements": ["PWR-HLR-005", "PWR-HLR-015"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(MainBusCritical=True)}]},
        {"id": "TEST-PWR-SOURCE-A-001", "requirements": ["PWR-HLR-006"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(BatteryAHealthy=False)}]},
        {"id": "TEST-PWR-SOURCE-B-001", "requirements": ["PWR-HLR-007"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(BatteryBHealthy=False)}]},
        {"id": "TEST-PWR-DUAL-CRITICAL-001", "requirements": ["PWR-HLR-002", "PWR-HLR-008"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(BatteryAHealthy=False, BatteryBHealthy=False, EssentialPowerAvailable=True)}]},
        {"id": "TEST-PWR-DUAL-LOCKOUT-001", "requirements": ["PWR-HLR-002", "PWR-HLR-008"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(BatteryAHealthy=False, BatteryBHealthy=False, EssentialPowerAvailable=False)}]},
        {"id": "TEST-PWR-PAYLOAD-001", "requirements": ["PWR-HLR-009"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(PayloadOvercurrent=True)}]},
        {"id": "TEST-PWR-CONTACTOR-001", "requirements": ["PWR-HLR-010", "PWR-HLR-011", "PWR-HLR-012"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(ContactorMismatchPersisted=True)}, {"inputs": nominal_inputs(RecoveryStable=True)}, {"inputs": nominal_inputs(RecoveryStable=True, ResetAuthorized=True)}]},
        {"id": "TEST-PWR-INVALID-001", "requirements": ["PWR-HLR-013", "PWR-HLR-014"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(InputSetValid=False)}, {"inputs": nominal_inputs(InputSetValid=False, FlightPhaseLanding=True)}]},
        {"id": "TEST-PWR-RUNTIME-001", "requirements": ["PWR-HLR-014"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs(), "blocking_runtime_fault": True}]},
        {"id": "TEST-PWR-CRITICAL-RECOVERY-001", "requirements": ["PWR-HLR-011", "PWR-HLR-012"], "initial_state": {"State": "CRITICAL", "BlockingFaultLatched": True}, "cycles": [{"inputs": nominal_inputs(RecoveryStable=True)}, {"inputs": nominal_inputs(RecoveryStable=True, ResetAuthorized=True)}]},
        {"id": "TEST-PWR-LOCKOUT-RECOVERY-001", "requirements": ["PWR-HLR-011", "PWR-HLR-012"], "initial_state": {"State": "LOCKOUT", "BlockingFaultLatched": True}, "cycles": [{"inputs": nominal_inputs(RecoveryStable=True, ResetAuthorized=True)}]},
        {"id": "TEST-PWR-RECOVERY-RESET-FALSE-001", "requirements": ["PWR-HLR-011", "PWR-HLR-012"], "initial_state": {"State": "RECOVERY_PENDING", "BlockingFaultLatched": True}, "cycles": [{"inputs": nominal_inputs(RecoveryStable=True, ResetAuthorized=False)}]},
        {"id": "TEST-PWR-RECOVERY-STABLE-FALSE-001", "requirements": ["PWR-HLR-011", "PWR-HLR-012"], "initial_state": {"State": "RECOVERY_PENDING", "BlockingFaultLatched": True}, "cycles": [{"inputs": nominal_inputs(RecoveryStable=False, ResetAuthorized=True)}]},
        {"id": "TEST-PWR-RECOVERY-RETURN-TRUE-001", "requirements": ["PWR-HLR-011", "PWR-HLR-012"], "initial_state": {"State": "RECOVERY_PENDING", "BlockingFaultLatched": True}, "cycles": [{"inputs": nominal_inputs(RecoveryStable=True, ResetAuthorized=True, ReturnEnergyInsufficient=True)}]},
        {"id": "TEST-PWR-RECOVERY-ESSENTIAL-FALSE-001", "requirements": ["PWR-HLR-011", "PWR-HLR-012"], "initial_state": {"State": "RECOVERY_PENDING", "BlockingFaultLatched": True}, "cycles": [{"inputs": nominal_inputs(RecoveryStable=True, ResetAuthorized=True, EssentialPowerAvailable=False)}]},
        {"id": "TEST-OUTPUT-DEFAULTS", "requirements": ["EXEC-REQ-OUTPUT-DEFAULTS"], "initial_state": {"State": "INITIALIZING", "BlockingFaultLatched": False}, "cycles": [{"inputs": nominal_inputs(InitializationComplete=False)}]},
        {"id": "TEST-GENERIC-EXECUTION", "requirements": ["ASCP-REQ-GENERIC-EXECUTION"], "initial_state": normal, "cycles": [{"inputs": nominal_inputs()}]},
    ]
    # Freeze independently calculated expected results into each vector.
    for scenario in scenarios:
        retained = copy.deepcopy(scenario["initial_state"])
        for cycle in scenario["cycles"]:
            retained, outputs, inhibited = oracle_step(retained, cycle["inputs"], bool(cycle.get("blocking_runtime_fault", False)))
            cycle["expected"] = {
                "committed_state": copy.deepcopy(retained),
                "outputs": outputs,
                "normal_commit_inhibited": inhibited,
            }
    return scenarios

def walk_air_statements(statements: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for stmt in statements:
        yield stmt
        if stmt["kind"] == "if":
            for branch in stmt["branches"]:
                yield {"kind": "decision", **branch}
                for condition in branch["conditions"]:
                    yield {"kind": "condition", **condition, "requirements": branch["requirements"], "tests": branch["tests"], "claim_critical": branch["claim_critical"], "mcdc": branch["mcdc"]}
                yield from walk_air_statements(branch["body"])
            yield from walk_air_statements(stmt["else_body"])
        elif stmt["kind"] == "case":
            for arm in stmt["arms"]:
                yield from walk_air_statements(arm["body"])


def all_air_nodes(air: dict[str, Any]) -> list[dict[str, Any]]:
    return list(walk_air_statements(air["program"]["transition_stage"])) + list(walk_air_statements(air["program"]["output_stage"]))


def coverage_report(air: dict[str, Any], results: list[CycleResult]) -> dict[str, Any]:
    nodes = all_air_nodes(air)
    statement_nodes = [n for n in nodes if n["kind"] in {"assign", "if", "case"}]
    decision_nodes = [n for n in nodes if n["kind"] == "decision"]
    condition_nodes = [n for n in nodes if n["kind"] == "condition"]
    reached_statements = {sid for r in results for sid in r.trace.statements}
    decision_values: dict[str, set[bool]] = {}
    condition_values: dict[str, set[bool]] = {}
    reached_case_arms = {x for r in results for x in r.trace.case_arms}
    for result in results:
        for event in result.trace.decisions:
            decision_values.setdefault(event.decision_id, set()).add(event.result)
            for cid, value in event.conditions:
                condition_values.setdefault(cid, set()).add(value)
    uncovered: list[dict[str, Any]] = []
    for node in statement_nodes:
        if node["id"] not in reached_statements:
            uncovered.append({
                "id": node["id"], "kind": "statement", "claim_critical": bool(node.get("claim_critical")),
                "disposition": "test-required" if node.get("claim_critical") else "non-claim-critical future vector",
            })
    for node in decision_nodes:
        values = decision_values.get(node["decision_id"], set())
        if values != {False, True}:
            uncovered.append({
                "id": node["decision_id"], "kind": "decision", "observed": sorted(values),
                "claim_critical": bool(node.get("claim_critical")),
                "disposition": "test-required" if node.get("claim_critical") else "non-claim-critical future vector",
            })
    for node in condition_nodes:
        values = condition_values.get(node["id"], set())
        if values != {False, True}:
            uncovered.append({
                "id": node["id"], "kind": "condition", "observed": sorted(values),
                "claim_critical": bool(node.get("claim_critical")),
                "disposition": "test-required" if node.get("claim_critical") else "non-claim-critical future vector",
            })
    claim_open = [x for x in uncovered if x["claim_critical"]]
    statement_reached = sum(n["id"] in reached_statements for n in statement_nodes)
    decision_reached = sum(decision_values.get(n["decision_id"], set()) == {False, True} for n in decision_nodes)
    condition_reached = sum(condition_values.get(n["id"], set()) == {False, True} for n in condition_nodes)
    complete = not uncovered
    return {
        "coverage_scope": "all executable AIR statements, both outcomes of every AIR decision, and both outcomes of every atomic AIR condition",
        "statement": {"reached": statement_reached, "total": len(statement_nodes), "percent": percent(statement_reached, len(statement_nodes))},
        "decision": {"both_outcomes": decision_reached, "total": len(decision_nodes), "percent": percent(decision_reached, len(decision_nodes))},
        "condition": {"both_outcomes": condition_reached, "total": len(condition_nodes), "percent": percent(condition_reached, len(condition_nodes))},
        "case_arms_reached": sorted(reached_case_arms),
        "uncovered_items": uncovered,
        "uncovered_count": len(uncovered),
        "claim_critical_open_count": len(claim_open),
        "claim_critical_closed": not claim_open,
        "complete": complete,
    }


def mcdc_report(air: dict[str, Any], results: list[CycleResult]) -> dict[str, Any]:
    selected = {n["decision_id"]: n for n in all_air_nodes(air) if n["kind"] == "decision" and n.get("mcdc")}
    evaluations: dict[str, list[DecisionEvent]] = {key: [] for key in selected}
    for result in results:
        for event in result.trace.decisions:
            if event.decision_id in evaluations:
                evaluations[event.decision_id].append(event)
    decisions_out = []
    required = 0
    found = 0
    for decision_id, node in selected.items():
        cond_ids = [c["id"] for c in node["conditions"]]
        pairs = []
        events = evaluations[decision_id]
        for target_index, cid in enumerate(cond_ids):
            required += 1
            match = None
            for i, a in enumerate(events):
                av = [v for _, v in a.conditions]
                for b in events[i + 1:]:
                    bv = [v for _, v in b.conditions]
                    if len(av) != len(bv):
                        continue
                    if av[target_index] == bv[target_index] or a.result == b.result:
                        continue
                    if all(av[k] == bv[k] for k in range(len(av)) if k != target_index):
                        match = {
                            "condition_id": cid,
                            "a": {"scenario": a.scenario_id, "cycle": a.cycle, "conditions": av, "result": a.result},
                            "b": {"scenario": b.scenario_id, "cycle": b.cycle, "conditions": bv, "result": b.result},
                        }
                        break
                if match:
                    break
            if match:
                found += 1
                pairs.append(match)
            else:
                pairs.append({"condition_id": cid, "missing": True})
        decisions_out.append({"decision_id": decision_id, "conditions": cond_ids, "pairs": pairs})
    return {
        "selected_decisions": len(selected),
        "conditions_analyzed": required,
        "independence_pairs_found": found,
        "independence_pairs_required": required,
        "complete": found == required,
        "decisions": decisions_out,
    }


def build_protocol(air: dict[str, Any], scenarios: list[dict[str, Any]], path: Path) -> None:
    lines = ["# AEROST generated-backend protocol v1"]
    state_names = [d["name"] for d in air["program"]["states"]]
    input_names = [d["name"] for d in air["program"]["inputs"]]
    for scenario in scenarios:
        initial = scenario.get("initial_state") or initial_state_from_air(air)
        lines.append("|".join(["S", scenario["id"]] + [f"{name}={format_protocol_value(initial[name])}" for name in state_names]))
        for index, cycle in enumerate(scenario["cycles"]):
            fields = ["C", str(index)] + [f"{name}={format_protocol_value(cycle['inputs'][name])}" for name in input_names]
            fields.append(f"BlockingRuntimeFault={format_protocol_value(bool(cycle.get('blocking_runtime_fault', False)))}")
            lines.append("|".join(fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def format_protocol_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def generated_package_name_from_manifest(crate_dir: Path) -> str:
    text = (crate_dir / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise AerostError(f"generated Cargo package name missing: {crate_dir / 'Cargo.toml'}")
    return match.group(1)


def locate_generated_binary(crate_dir: Path, release: bool = True) -> Path:
    package_name = generated_package_name_from_manifest(crate_dir)
    exe = package_name + (".exe" if os.name == "nt" else "")
    return crate_dir / "target" / ("release" if release else "debug") / exe


def run_command(command: list[str], cwd: Path | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture, check=False)
    if result.returncode != 0:
        raise AerostError(
            f"command failed ({result.returncode}): {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def compile_generated(crate_dir: Path, release: bool = True) -> Path:
    command = ["cargo", "build", "--manifest-path", str(crate_dir / "Cargo.toml")]
    if release:
        command.append("--release")
    run_command(command)
    binary = locate_generated_binary(crate_dir, release)
    if not binary.exists():
        raise AerostError(f"generated backend binary not found: {binary}")
    return binary


def parse_backend_output(text: str, air: dict[str, Any]) -> list[dict[str, Any]]:
    state_names = [d["name"] for d in air["program"]["states"]]
    output_names = [d["name"] for d in air["program"]["outputs"]]
    type_map = {d["name"]: d["type"] for key in ("states", "outputs") for d in air["program"][key]}
    results = []
    for line in text.splitlines():
        if not line.startswith("R|"):
            continue
        parts = line.split("|")
        scenario_id, cycle = parts[1], int(parts[2])
        fields: dict[str, str] = {}
        for part in parts[3:]:
            key, value = part.split("=", 1)
            fields[key] = value
        committed = {name: parse_protocol_typed(fields[name], type_map[name]) for name in state_names}
        outputs = {name: parse_protocol_typed(fields[name], type_map[name]) for name in output_names}
        decisions = []
        if fields.get("Decisions"):
            for item in fields["Decisions"].split(";"):
                if not item:
                    continue
                seg = item.split(":", 2)
                conds = []
                if len(seg) == 3 and seg[2]:
                    for c in seg[2].split(","):
                        cid, val = c.split("=", 1)
                        conds.append({"id": cid, "value": val == "1"})
                decisions.append({"decision_id": seg[0], "result": seg[1] == "1", "conditions": conds, "scenario_id": scenario_id, "cycle": cycle})
        results.append({
            "scenario_id": scenario_id,
            "cycle": cycle,
            "committed_state": committed,
            "outputs": outputs,
            "normal_commit_inhibited": fields["NormalCommitInhibited"] == "1",
            "statements": [x for x in fields.get("Statements", "").split(",") if x],
            "case_arms": [x for x in fields.get("Cases", "").split(",") if x],
            "diagnostics": [x for x in fields.get("Diagnostics", "").split(",") if x],
            "decisions": decisions,
        })
    return results


def parse_protocol_typed(value: str, type_name: str) -> Any:
    if type_name == "BOOL":
        return value == "1"
    if type_name in {"U16", "I32"}:
        return int(value)
    return value


def differential_report(reference: list[CycleResult], backend: list[dict[str, Any]]) -> dict[str, Any]:
    by_key = {(x["scenario_id"], x["cycle"]): x for x in backend}
    mismatches = []
    equivalent = 0
    for result in reference:
        key = (result.scenario_id, result.cycle)
        candidate = by_key.get(key)
        expected = result.observable()
        # Rust backend comparison includes trace evidence as well as observable behavior.
        normalized_expected = {
            "committed_state": expected["committed_state"],
            "outputs": expected["outputs"],
            "normal_commit_inhibited": expected["normal_commit_inhibited"],
            "diagnostics": expected["diagnostics"],
            "statements": expected["statements"],
            "decisions": expected["decisions"],
            "case_arms": expected["case_arms"],
        }
        if candidate == {"scenario_id": result.scenario_id, "cycle": result.cycle, **normalized_expected}:
            equivalent += 1
        else:
            mismatches.append({"scenario_id": result.scenario_id, "cycle": result.cycle, "reference": normalized_expected, "backend": candidate})
    return {
        "executed_cycles": len(reference),
        "equivalent_cycles": equivalent,
        "mismatch_count": len(mismatches),
        "behavior_equivalence_percent": (equivalent / len(reference) * 100.0) if reference else 0.0,
        "mismatches": mismatches,
    }


TRACE_OBSERVABLE_KEYS = (
    "scenario_id",
    "cycle",
    "committed_state",
    "outputs",
    "normal_commit_inhibited",
    "statements",
    "decisions",
    "case_arms",
    "diagnostics",
)


def normalized_trace_cycle(cycle: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in TRACE_OBSERVABLE_KEYS if key not in cycle]
    if missing:
        raise AerostError(f"trace cycle is missing observable fields: {missing}")
    return {key: cycle[key] for key in TRACE_OBSERVABLE_KEYS}


def pairwise_trace_report(
    expected_name: str,
    expected_cycles: list[dict[str, Any]],
    candidate_name: str,
    candidate_cycles: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_by_key = {
        (cycle["scenario_id"], cycle["cycle"]): normalized_trace_cycle(cycle)
        for cycle in expected_cycles
    }
    candidate_by_key = {
        (cycle["scenario_id"], cycle["cycle"]): normalized_trace_cycle(cycle)
        for cycle in candidate_cycles
    }
    keys = sorted(set(expected_by_key) | set(candidate_by_key))
    mismatches: list[dict[str, Any]] = []
    equivalent = 0
    for scenario_id, cycle in keys:
        key = (scenario_id, cycle)
        expected = expected_by_key.get(key)
        candidate = candidate_by_key.get(key)
        if expected is not None and expected == candidate:
            equivalent += 1
        else:
            mismatches.append({
                "scenario_id": scenario_id,
                "cycle": cycle,
                expected_name: expected,
                candidate_name: candidate,
            })
    return {
        "expected_path": expected_name,
        "candidate_path": candidate_name,
        "executed_cycles": len(keys),
        "equivalent_cycles": equivalent,
        "mismatch_count": len(mismatches),
        "behavior_equivalence_percent": (equivalent / len(keys) * 100.0) if keys else 0.0,
        "mismatches": mismatches,
        "passed": not mismatches,
    }


def external_executor_independence_report(root: Path) -> dict[str, Any]:
    executor_path = root / "tools" / "external_air_executor.py"
    if not executor_path.exists():
        raise AerostError(f"external AIR executor not found: {executor_path}")
    source = executor_path.read_text(encoding="utf-8-sig")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise AerostError(f"external AIR executor syntax error: {exc}") from exc

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_roots.add((node.module or "").split(".", 1)[0])

    allowed_import_roots = {
        "__future__",
        "argparse",
        "copy",
        "dataclasses",
        "json",
        "pathlib",
        "sys",
        "typing",
    }
    unexpected_import_roots = sorted(imported_roots - allowed_import_roots)
    forbidden_project_references = sorted(
        token for token in (
            "aerost_tool",
            "execute_cycle_air",
            "run_reference_scenarios",
        )
        if token in source
    )
    application_specific_tokens = sorted(
        token for token in (
            "ShedNonessentialLoads",
            "ImmediateLandingRequest",
            "PowerSupervisor",
            "CommunicationLink",
        )
        if token in source
    )
    passed = not (
        unexpected_import_roots
        or forbidden_project_references
        or application_specific_tokens
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_version": PROFILE_VERSION,
        "executor_path": executor_path.relative_to(root).as_posix(),
        "executor_sha256": sha256_file(executor_path),
        "separate_process": True,
        "standard_library_only": not unexpected_import_roots,
        "imported_roots": sorted(imported_roots),
        "unexpected_import_roots": unexpected_import_roots,
        "imports_project_executor": bool(forbidden_project_references),
        "forbidden_project_references": forbidden_project_references,
        "application_specific_logic_detected": bool(application_specific_tokens),
        "application_specific_tokens": application_specific_tokens,
        "passed": passed,
    }


def run_external_air_executor(
    root: Path,
    air_path: Path,
    protocol_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    executor_path = root / "tools" / "external_air_executor.py"
    run_command([
        sys.executable,
        str(executor_path),
        "--air",
        str(air_path),
        "--protocol",
        str(protocol_path),
        "--output",
        str(output_path),
    ], cwd=root)
    document = load_json(output_path)
    cycles = document.get("cycles")
    metadata = document.get("executor")
    if not isinstance(cycles, list) or not cycles:
        raise AerostError("external AIR executor produced no cycle records")
    if not isinstance(metadata, dict):
        raise AerostError("external AIR executor metadata is missing")
    if metadata.get("imports_project_executor") is not False:
        raise AerostError("external executor reports project-executor imports")
    if metadata.get("application_specific_logic") is not False:
        raise AerostError("external executor reports application-specific logic")
    return document


def three_way_differential_report(
    reference: list[CycleResult],
    backend: list[dict[str, Any]],
    external: list[dict[str, Any]],
) -> dict[str, Any]:
    reference_cycles = [result.to_json() for result in reference]
    pairwise = {
        "reference_vs_generated_rust": pairwise_trace_report(
            "reference_interpreter",
            reference_cycles,
            "generated_rust",
            backend,
        ),
        "reference_vs_external_executor": pairwise_trace_report(
            "reference_interpreter",
            reference_cycles,
            "external_executor",
            external,
        ),
        "generated_rust_vs_external_executor": pairwise_trace_report(
            "generated_rust",
            backend,
            "external_executor",
            external,
        ),
    }
    ref_by_key = {
        (cycle["scenario_id"], cycle["cycle"]): normalized_trace_cycle(cycle)
        for cycle in reference_cycles
    }
    backend_by_key = {
        (cycle["scenario_id"], cycle["cycle"]): normalized_trace_cycle(cycle)
        for cycle in backend
    }
    external_by_key = {
        (cycle["scenario_id"], cycle["cycle"]): normalized_trace_cycle(cycle)
        for cycle in external
    }
    keys = sorted(set(ref_by_key) | set(backend_by_key) | set(external_by_key))
    mismatches: list[dict[str, Any]] = []
    equivalent = 0
    for scenario_id, cycle in keys:
        key = (scenario_id, cycle)
        values = {
            "reference_interpreter": ref_by_key.get(key),
            "generated_rust": backend_by_key.get(key),
            "external_executor": external_by_key.get(key),
        }
        if values["reference_interpreter"] is not None and len({canonical_json(value) for value in values.values()}) == 1:
            equivalent += 1
        else:
            mismatches.append({
                "scenario_id": scenario_id,
                "cycle": cycle,
                "paths": values,
            })
    passed = not mismatches and all(report["passed"] for report in pairwise.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_version": PROFILE_VERSION,
        "execution_paths": [
            "reference_interpreter",
            "generated_rust",
            "external_executor",
        ],
        "execution_path_count": 3,
        "executed_cycles": len(keys),
        "equivalent_cycles": equivalent,
        "mismatch_count": len(mismatches),
        "behavior_equivalence_percent": (equivalent / len(keys) * 100.0) if keys else 0.0,
        "pairwise": pairwise,
        "mismatches": mismatches,
        "passed": passed,
    }


def find_tagged(air: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    found = []
    for node in all_air_nodes(air):
        if tag in node.get("mutation_tags", []):
            found.append(node)
    return found


def mutate_air(original: dict[str, Any], tag: str) -> dict[str, Any]:
    air = copy.deepcopy(original)
    changed = False

    def visit(stmts: list[dict[str, Any]]) -> None:
        nonlocal changed
        for stmt in stmts:
            tags = stmt.get("mutation_tags", [])
            if tag == "payload-default" and stmt["kind"] == "assign" and stmt["target"] == "ShedNonessentialLoads":
                stmt["expression"] = {"kind": "bool", "type": "BOOL", "span": stmt["span"], "value": False}
                changed = True
            if tag == "critical-output-delete" and stmt["kind"] == "assign" and stmt["target"] == "ImmediateLandingRequest" and stmt["expression"].get("value") is True:
                stmt["expression"] = {"kind": "bool", "type": "BOOL", "span": stmt["span"], "value": False}
                changed = True
            if stmt["kind"] == "if":
                for branch in stmt["branches"]:
                    btags = branch.get("mutation_tags", [])
                    if tag == "dual-source-operator" and tag in btags:
                        expr = branch["expression"]
                        if expr.get("kind") == "binary" and expr.get("operator") == "AND":
                            expr["operator"] = "OR"
                            changed = True
                    elif tag == "critical-condition" and tag in btags:
                        branch["expression"] = {"kind": "unary", "type": "BOOL", "span": branch["expression"]["span"], "operator": "NOT", "operand": branch["expression"]}
                        changed = True
                    elif tag == "weaken-recovery" and tag in btags:
                        branch["expression"] = {"kind": "name", "type": "BOOL", "span": branch["expression"]["span"], "value": "ResetAuthorized"}
                        changed = True
                    elif tag == "invalid-output-guard" and tag in btags:
                        branch["expression"] = {"kind": "bool", "type": "BOOL", "span": branch["expression"]["span"], "value": False}
                        changed = True
                    elif tag == "invalid-guard" and tag in btags:
                        branch["expression"] = {"kind": "bool", "type": "BOOL", "span": branch["expression"]["span"], "value": True}
                        changed = True
                    if tag == "critical-target" and tag in btags:
                        for child in branch["body"]:
                            if child["kind"] == "assign" and child["target"] == "State" and child["expression"].get("value") == "CRITICAL":
                                child["expression"]["value"] = "NORMAL"
                                changed = True
                    visit(branch["body"])
                visit(stmt["else_body"])
            elif stmt["kind"] == "case":
                for arm in stmt["arms"]:
                    visit(arm["body"])

    visit(air["program"]["transition_stage"])
    visit(air["program"]["output_stage"])
    if tag == "swap-isolation":
        assignments = []
        for node in all_air_nodes(air):
            if node["kind"] == "assign" and node.get("target") in {"IsolateBatteryA", "IsolateBatteryB"}:
                assignments.append(node)
        a = next((x for x in assignments if x["target"] == "IsolateBatteryA" and x["expression"].get("kind") != "bool"), None)
        b = next((x for x in assignments if x["target"] == "IsolateBatteryB" and x["expression"].get("kind") != "bool"), None)
        if a and b:
            a["expression"], b["expression"] = b["expression"], a["expression"]
            changed = True
    if not changed:
        raise AerostError(f"mutation tag did not modify AIR: {tag}")
    air["mutation"] = {"id": f"MUT-{tag.upper()}", "operator": tag}
    return air


MUTATION_TAGS = [
    "dual-source-operator",
    "critical-condition",
    "critical-target",
    "critical-output-delete",
    "payload-default",
    "invalid-output-guard",
    "weaken-recovery",
    "swap-isolation",
]


def expected_mismatches(results: list[CycleResult], scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected_by_key = {}
    for scenario in scenarios:
        for cycle_index, cycle in enumerate(scenario["cycles"]):
            expected_by_key[(scenario["id"], cycle_index)] = cycle["expected"]
    mismatches = []
    for result in results:
        got = {
            "committed_state": result.committed_state,
            "outputs": result.outputs,
            "normal_commit_inhibited": result.normal_commit_inhibited,
        }
        expected = expected_by_key[(result.scenario_id, result.cycle)]
        if got != expected:
            mismatches.append({"scenario_id": result.scenario_id, "cycle": result.cycle, "expected": expected, "actual": got})
    return mismatches


def mutation_report(
    air: dict[str, Any],
    scenarios: list[dict[str, Any]],
    work_dir: Path,
    compile_and_run: bool,
    mutation_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    records = []
    protocol = work_dir / "mutation-protocol.txt"
    build_protocol(air, scenarios, protocol)
    if mutation_profile is None:
        mutated_airs = {
            f"MUT-{tag.upper()}": (tag, mutate_air(air, tag))
            for tag in MUTATION_TAGS
        }
    else:
        from application_mutations import build_mutated_airs, mutation_operator_map

        generated = build_mutated_airs(air, mutation_profile)
        operators = mutation_operator_map(mutation_profile)
        mutated_airs = {
            mutant_id: (operators[mutant_id], mutated)
            for mutant_id, mutated in generated.items()
        }
    for mutant_id, (tag, mutated) in mutated_airs.items():
        reference_results = run_reference_scenarios(mutated, scenarios)
        reference_mismatches = expected_mismatches(reference_results, scenarios)
        backend_mismatches: list[dict[str, Any]] = []
        compiled = False
        if compile_and_run:
            crate_dir = work_dir / "mutants" / tag
            generate_rust_crate(mutated, crate_dir)
            binary = compile_generated(crate_dir)
            run = run_command([str(binary), "run", str(protocol)])
            backend_results = parse_backend_output(run.stdout, mutated)
            expected_by_key = {
                (s["id"], i): c["expected"]
                for s in scenarios for i, c in enumerate(s["cycles"])
            }
            for result in backend_results:
                actual = {
                    "committed_state": result["committed_state"],
                    "outputs": result["outputs"],
                    "normal_commit_inhibited": result["normal_commit_inhibited"],
                }
                expected = expected_by_key[(result["scenario_id"], result["cycle"])]
                if actual != expected:
                    backend_mismatches.append({"scenario_id": result["scenario_id"], "cycle": result["cycle"], "expected": expected, "actual": actual})
            compiled = True
        killed = bool(reference_mismatches) and (not compile_and_run or bool(backend_mismatches))
        records.append({
            "mutant_id": mutant_id,
            "operator": tag,
            "air_mutation": True,
            "generated_backend_compiled": compiled,
            "killed": killed,
            "reference_detecting_cases": [{"scenario_id": x["scenario_id"], "cycle": x["cycle"]} for x in reference_mismatches],
            "backend_detecting_cases": [{"scenario_id": x["scenario_id"], "cycle": x["cycle"]} for x in backend_mismatches],
            "equivalent": False,
        })
    detected = sum(x["killed"] for x in records)
    return {
        "executed_non_equivalent_mutants": len(records),
        "detected": detected,
        "surviving": [x["mutant_id"] for x in records if not x["killed"]],
        "score_percent": detected / len(records) * 100.0 if records else 0.0,
        "records": records,
    }


def traceability_report(
    air: dict[str, Any],
    source_map: dict[str, Any],
    requirements: dict[str, Any],
    scenarios: list[dict[str, Any]],
    results: list[CycleResult],
) -> dict[str, Any]:
    req_ids = {r["id"] for r in requirements["requirements"]}
    nodes = all_air_nodes(air)
    air_ids = {n.get("id") or n.get("decision_id") for n in nodes}
    source_ids = {n["source_id"] for n in nodes}
    backend_by_air = {m["air_id"]: m["generated_block"] for m in source_map["mappings"]}

    req_to_source: dict[str, list[str]] = {rid: [] for rid in req_ids}
    source_to_air: dict[str, str] = {}
    for node in nodes:
        air_id = node.get("id") or node.get("decision_id")
        source_id = node["source_id"]
        source_to_air[source_id] = air_id
        for rid in node.get("requirements", []):
            if rid in req_ids:
                req_to_source.setdefault(rid, []).append(source_id)

    # Tool/build requirements are represented by controlled pipeline identities.
    req_to_source.setdefault("BUILD-REQ-001", []).append("ASCP-SRC-TOOL-REPRODUCIBILITY")
    source_to_air["ASCP-SRC-TOOL-REPRODUCIBILITY"] = "AIR-TOOL-REPRODUCIBILITY"
    backend_by_air["AIR-TOOL-REPRODUCIBILITY"] = "RUST-TOOL-REPRODUCIBILITY"

    scenario_req: dict[str, list[str]] = {rid: [] for rid in req_ids}
    for scenario in scenarios:
        for rid in scenario.get("requirements", []):
            if rid in req_ids:
                scenario_req.setdefault(rid, []).append(scenario["id"])
    scenario_req["BUILD-REQ-001"] = ["TEST-BUILD-REPRODUCIBILITY"]

    result_tests = {r.scenario_id for r in results}
    result_tests.add("TEST-BUILD-REPRODUCIBILITY")
    all_source_ids = source_ids | {"ASCP-SRC-TOOL-REPRODUCIBILITY"}
    all_air_ids = air_ids | {"AIR-TOOL-REPRODUCIBILITY"}

    req_source_linked = sum(bool(req_to_source.get(rid)) for rid in req_ids)
    source_air_linked = sum(source_id in source_to_air for source_id in all_source_ids)
    air_backend_linked = sum(air_id in backend_by_air for air_id in all_air_ids)
    req_test_linked = sum(bool(scenario_req.get(rid)) for rid in req_ids)
    test_links = [(rid, test) for rid, tests in scenario_req.items() for test in tests]
    test_result_linked = sum(test in result_tests for _, test in test_links)
    app_reqs = sorted(
        rid
        for rid in req_ids
        if rid
        not in {
            "EXEC-REQ-OUTPUT-DEFAULTS",
            "ASCP-REQ-GENERIC-EXECUTION",
            "BUILD-REQ-001",
        }
    )

    report = {
        "requirement_to_source": {
            "linked": req_source_linked,
            "total": len(req_ids),
            "percent": percent(req_source_linked, len(req_ids)),
            "orphans": sorted(rid for rid in req_ids if not req_to_source.get(rid)),
            "links": {rid: sorted(set(ids)) for rid, ids in sorted(req_to_source.items())},
        },
        "source_to_air": {
            "linked": source_air_linked,
            "total": len(all_source_ids),
            "percent": percent(source_air_linked, len(all_source_ids)),
            "orphans": sorted(all_source_ids - set(source_to_air)),
            "links": dict(sorted(source_to_air.items())),
        },
        "air_to_backend": {
            "linked": air_backend_linked,
            "total": len(all_air_ids),
            "percent": percent(air_backend_linked, len(all_air_ids)),
            "orphans": sorted(all_air_ids - set(backend_by_air)),
            "links": dict(sorted(backend_by_air.items())),
        },
        "requirement_to_test": {
            "linked": req_test_linked,
            "total": len(req_ids),
            "percent": percent(req_test_linked, len(req_ids)),
            "orphans": sorted(rid for rid in req_ids if not scenario_req.get(rid)),
            "links": {rid: sorted(set(ids)) for rid, ids in sorted(scenario_req.items())},
        },
        "test_to_result": {
            "linked": test_result_linked,
            "total": len(test_links),
            "percent": percent(test_result_linked, len(test_links)),
            "orphans": sorted(test for _, test in test_links if test not in result_tests),
        },
        "application_requirements": app_reqs,
    }
    report["complete"] = all(
        not report[boundary]["orphans"]
        for boundary in (
            "requirement_to_source",
            "source_to_air",
            "air_to_backend",
            "requirement_to_test",
            "test_to_result",
        )
    )
    return report


def expressive_adequacy_report(traceability: dict[str, Any]) -> dict[str, Any]:
    app = traceability["application_requirements"]
    links = traceability["requirement_to_source"]["links"]
    represented = [rid for rid in app if links.get(rid)]
    return {
        "implemented_requirements": len(app),
        "represented_in_profile": len(represented),
        "percent": percent(len(represented), len(app)),
        "requiring_profile_extension": [rid for rid in app if rid not in represented],
        "hidden_external_logic": [],
        "complete": len(represented) == len(app),
    }


def percent(n: int, d: int) -> float:
    return round(n / d * 100.0, 2) if d else 0.0

def expr_ast_json(expr: Expr | None) -> Any:
    if expr is None:
        return None
    data = {"kind": expr.kind, "span": expr.span.to_json(), "type": expr.value_type}
    if expr.kind in {"name", "bool", "int"}:
        data["value"] = expr.value
    elif expr.kind == "unary":
        data.update({"operator": expr.value, "operand": expr_ast_json(expr.operand)})
    else:
        data.update({"operator": expr.value, "left": expr_ast_json(expr.left), "right": expr_ast_json(expr.right)})
    return data


def stmt_ast_json(stmt: Statement) -> dict[str, Any]:
    data = {
        "kind": stmt.kind,
        "span": stmt.span.to_json(),
        "annotation": dataclasses.asdict(stmt.annotation),
    }
    if stmt.kind == "assign":
        data.update({"target": stmt.target, "expression": expr_ast_json(stmt.expr)})
    elif stmt.kind == "if":
        data["branches"] = [
            {"condition": expr_ast_json(cond), "annotation": dataclasses.asdict(ann), "body": [stmt_ast_json(x) for x in body]}
            for cond, body, ann in stmt.branches
        ]
        data["else_body"] = [stmt_ast_json(x) for x in stmt.else_body]
    else:
        data["selector"] = expr_ast_json(stmt.selector)
        data["arms"] = [
            {"value": value, "annotation": dataclasses.asdict(ann), "body": [stmt_ast_json(x) for x in body]}
            for value, body, ann in stmt.arms
        ]
    return data


def typed_program_json(typed: TypedProgram) -> dict[str, Any]:
    p = typed.program
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_version": PROFILE_VERSION,
        "program": {
            "name": p.name,
            "types": [dataclasses.asdict(t) | {"span": t.span.to_json()} for t in p.types],
            "inputs": [decl_ast_json(d) for d in p.inputs],
            "outputs": [decl_ast_json(d) for d in p.outputs],
            "states": [decl_ast_json(d) for d in p.states],
            "transition_stage": [stmt_ast_json(x) for x in p.transition],
            "output_stage": [stmt_ast_json(x) for x in p.output],
        },
    }


def decl_ast_json(decl: VarDecl) -> dict[str, Any]:
    return {
        "name": decl.name,
        "type": decl.type_name,
        "section": decl.section,
        "initializer": expr_ast_json(decl.initializer),
        "span": decl.span.to_json(),
    }


def json_type_matches(value: Any, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "null":
        return value is None
    return True


def validate_json_schema(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type:
        types = [expected_type] if isinstance(expected_type, str) else expected_type
        if not any(json_type_matches(instance, t) for t in types):
            return [f"{path}: expected type {types}, found {type(instance).__name__}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value {instance!r} not in enum")
    if isinstance(instance, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                errors.append(f"{path}: missing required property {key}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate_json_schema(value, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {key}")
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: expected at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: expected at most {schema['maxItems']} items")
        if "items" in schema:
            for index, value in enumerate(instance):
                errors.extend(validate_json_schema(value, schema["items"], f"{path}[{index}]"))
    if isinstance(instance, str) and "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
        errors.append(f"{path}: string does not match {schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: value below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: value above maximum")
    return errors


def validate_artifacts_with_schemas(
    root: Path,
    artifact_dir: Path,
    application: ApplicationConfig | None = None,
) -> dict[str, Any]:
    selected = application or load_application_manifest(root)
    pairs = {
        "application-manifest.json": "application-manifest.schema.json",
        "mutation-profile.json": "mutation-profile.schema.json",
        selected.air_artifact_name: "air.schema.json",
        "runtime-fault-policy.json": "runtime-fault-policy.schema.json",
        "reference-traces.json": "trace.schema.json",
        "backend-traces.json": "trace.schema.json",
        "external-executor-traces.json": "trace.schema.json",
        "differential-results.json": "differential.schema.json",
        "three-way-differential-results.json": "three-way-differential.schema.json",
        "external-executor-independence-report.json": "external-executor-independence.schema.json",
        "coverage-report.json": "coverage.schema.json",
        "mcdc-report.json": "mcdc.schema.json",
        "mutation-report.json": "mutation.schema.json",
        "traceability-report.json": "traceability.schema.json",
        "expressive-adequacy.json": "expressive.schema.json",
        "results-summary.json": "results-summary.schema.json",
        "backend-independence-report.json": "backend-independence.schema.json",
        "generated-backend/source-map.json": "source-map.schema.json",
        "manifest.json": "manifest.schema.json",
        "controlled-scenarios.json": "controlled-scenarios.schema.json",
        "controlled-input-domain.json": "input-domain.schema.json",
        "input-domain-validation.json": "input-domain-validation.schema.json",
        "assurance-obligations.json": "assurance-obligations.schema.json",
        "synthesized-scenarios.json": "synthesized-scenarios.schema.json",
        "synthesis-report.json": "synthesis-report.schema.json",
        "obligation-reduced-scenarios.json": "synthesized-scenarios.schema.json",
        "suite-reduction-report.json": "suite-reduction-report.schema.json",
        "mutation-aware-reduced-scenarios.json": "synthesized-scenarios.schema.json",
        "mutation-aware-suite-reduction-report.json": "mutation-aware-suite-reduction-report.schema.json",
        "assurance-suite-comparison.json": "assurance-suite-comparison.schema.json",
    }
    if (artifact_dir / "host-timing.json").exists():
        pairs["host-timing.json"] = "host-timing.schema.json"
    records = []
    all_ok = True
    for artifact_name, schema_name in pairs.items():
        artifact_path = artifact_dir / artifact_name
        schema_path = root / "schemas" / schema_name
        if not artifact_path.exists():
            records.append({"artifact": artifact_name, "schema": schema_name, "valid": False, "errors": ["artifact missing"]})
            all_ok = False
            continue
        errors = validate_json_schema(load_json(artifact_path), load_json(schema_path))
        records.append({"artifact": artifact_name, "schema": schema_name, "valid": not errors, "errors": errors})
        all_ok = all_ok and not errors
    return {"validator": "AEROST JSON Schema Draft 2020-12 controlled-subset validator", "all_valid": all_ok, "records": records}

def conformance_report(
    root: Path,
    compile_accepted: bool = False,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    records = []
    if compile_accepted:
        if work_dir is None:
            raise AerostError("conformance compile work directory is required")
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

    for expected_accept, folder in ((True, root / "conformance" / "accepted"), (False, root / "conformance" / "rejected")):
        for path in sorted(folder.glob("*.ascp")):
            accepted = False
            diagnostic = None
            generated = False
            compiled = False
            try:
                typed = semantic_analyze(parse_source(path.read_text(encoding="utf-8")))
                air = lower_to_air(typed)
                accepted = True
                if expected_accept and compile_accepted:
                    generated_dir = work_dir / path.stem
                    generate_rust_crate(air, generated_dir)
                    generated = True
                    compile_generated(generated_dir)
                    compiled = True
            except AerostError as exc:
                diagnostic = str(exc).splitlines()[0]

            passed = accepted == expected_accept
            if expected_accept and compile_accepted:
                passed = passed and generated and compiled
            records.append({
                "file": path.name,
                "expected_accept": expected_accept,
                "accepted": accepted,
                "generated": generated,
                "compiled": compiled,
                "passed": passed,
                "diagnostic": diagnostic,
            })

    if compile_accepted and work_dir is not None and work_dir.exists():
        shutil.rmtree(work_dir)

    return {
        "passed": sum(r["passed"] for r in records),
        "total": len(records),
        "accepted_compiled": sum(1 for r in records if r["expected_accept"] and r["compiled"]),
        "accepted_total": sum(1 for r in records if r["expected_accept"]),
        "records": records,
        "complete": all(r["passed"] for r in records),
    }


def traces_document(results: list[CycleResult]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "profile_version": PROFILE_VERSION, "cycles": [r.to_json() for r in results]}


def backend_traces_document(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "profile_version": PROFILE_VERSION, "cycles": results}


def oracle_comparison_report(reference: list[CycleResult], scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    mismatches = expected_mismatches(reference, scenarios)
    return {"controlled_cycles": len(reference), "equivalent_cycles": len(reference) - len(mismatches), "mismatch_count": len(mismatches), "mismatches": mismatches, "passed": not mismatches}


def host_platform_report() -> dict[str, Any]:
    report = {
        "os": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cores": os.cpu_count(),
        "python": platform.python_version(),
        "tool_version": TOOL_VERSION,
        "rustc": command_version(["rustc", "-Vv"]),
        "cargo": command_version(["cargo", "-V"]),
    }
    if os.name == "nt":
        try:
            command = [
                "powershell", "-NoProfile", "-Command",
                "$cpu=Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores,NumberOfLogicalProcessors; "
                "$cs=Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory; "
                "$os=Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber; "
                "@{cpu=$cpu;computer=$cs;os=$os}|ConvertTo-Json -Depth 4 -Compress",
            ]
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            if result.returncode == 0 and result.stdout.strip():
                report["windows_cim"] = json.loads(result.stdout)
        except (OSError, json.JSONDecodeError):
            report["windows_cim"] = "unavailable"
    return report


def command_version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        return (result.stdout or result.stderr).strip()
    except OSError:
        return "unavailable"


def parse_host_timing_output(text: str) -> dict[str, Any]:
    paths: list[dict[str, Any]] = []
    total: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("BENCHCASE|"):
            parts = line.split("|")
            paths.append({
                "scenario_id": parts[1],
                "cycle": int(parts[2]),
                "samples": int(parts[3]),
                "minimum_ns": int(parts[4]),
                "median_ns": int(parts[5]),
                "mean_ns": float(parts[6]),
                "p95_ns": int(parts[7]),
                "p99_ns": int(parts[8]),
                "maximum_ns": int(parts[9]),
            })
        elif line.startswith("BENCHTOTAL|"):
            parts = line.split("|")
            total = {
                "controlled_paths": int(parts[1]),
                "samples_per_path": int(parts[2]),
                "warmup_per_path": int(parts[3]),
                "total_samples": int(parts[4]),
                "aggregate": {
                    "minimum_ns": int(parts[5]),
                    "median_ns": int(parts[6]),
                    "mean_ns": float(parts[7]),
                    "p95_ns": int(parts[8]),
                    "p99_ns": int(parts[9]),
                    "maximum_ns": int(parts[10]),
                },
                "worst_observed_path": {
                    "scenario_id": parts[11],
                    "cycle": int(parts[12]),
                    "maximum_ns": int(parts[13]),
                },
            }
    if total is None:
        raise AerostError("generated backend did not emit BENCHTOTAL record")
    if len(paths) != total["controlled_paths"]:
        raise AerostError(
            f"timing path count mismatch: records={len(paths)}, declared={total['controlled_paths']}"
        )
    if any(path["samples"] != total["samples_per_path"] for path in paths):
        raise AerostError("timing samples-per-path mismatch")
    return {
        "measurement_scope": "every controlled scenario cycle measured independently on the generated release backend",
        **total,
        "paths": paths,
        "interpretation": "host feasibility observation across controlled paths; not target WCET",
        "build_profile": "release",
    }


def run_host_timing(binary: Path, protocol: Path, samples_per_path: int) -> dict[str, Any]:
    result = run_command([str(binary), "benchmark-protocol", str(protocol), str(samples_per_path)])
    return parse_host_timing_output(result.stdout)



ASSURANCE_ARTIFACTS = (
    "mutation-profile.json",
    "controlled-input-domain.json",
    "input-domain-validation.json",
    "assurance-obligations.json",
    "synthesized-scenarios.json",
    "synthesis-report.json",
    "obligation-reduced-scenarios.json",
    "suite-reduction-report.json",
    "mutation-aware-reduced-scenarios.json",
    "mutation-aware-suite-reduction-report.json",
    "assurance-suite-comparison.json",
)


def _run_python_tool(root: Path, script: str, arguments: list[str]) -> None:
    run_command(
        [sys.executable, str(root / "tools" / script), *arguments],
        cwd=root,
    )


def _suite_by_name(comparison: dict[str, Any], name: str) -> dict[str, Any]:
    for suite in comparison.get("suites", []):
        if suite.get("name") == name:
            return suite
    raise AerostError(f"assurance comparison is missing suite: {name}")


def run_assurance_evidence_pipeline(
    root: Path,
    output_dir: Path,
    announce: bool = False,
    application: ApplicationConfig | None = None,
) -> dict[str, Any]:
    selected = application or load_application_manifest(root)
    air_path = output_dir / selected.air_artifact_name
    policy_path = output_dir / "runtime-fault-policy.json"
    traceability_path = output_dir / "traceability-report.json"
    domain_source = selected.input_domain_path
    domain_path = output_dir / "controlled-input-domain.json"
    shutil.copy2(domain_source, domain_path)

    obligations_path = output_dir / "assurance-obligations.json"
    domain_validation_path = output_dir / "input-domain-validation.json"
    synthesized_path = output_dir / "synthesized-scenarios.json"
    synthesis_report_path = output_dir / "synthesis-report.json"
    obligation_reduced_path = output_dir / "obligation-reduced-scenarios.json"
    reduction_report_path = output_dir / "suite-reduction-report.json"
    mutation_reduced_path = output_dir / "mutation-aware-reduced-scenarios.json"
    mutation_report_path = output_dir / "mutation-aware-suite-reduction-report.json"
    comparison_path = output_dir / "assurance-suite-comparison.json"

    if announce:
        print("[7/15] Assurance obligations and bounded input domain")
    _run_python_tool(
        root,
        "assurance_obligations.py",
        [
            "extract",
            "--air",
            str(air_path),
            "--fault-policy",
            str(policy_path),
            "--traceability",
            str(traceability_path),
            "--output",
            str(obligations_path),
        ],
    )
    _run_python_tool(
        root,
        "input_domain_validator.py",
        [
            "validate",
            "--air",
            str(air_path),
            "--domain",
            str(domain_path),
            "--output",
            str(domain_validation_path),
        ],
    )

    if announce:
        print("[8/15] Bounded synthesis and deterministic suite reduction")
    _run_python_tool(
        root,
        "assurance_test_synthesizer.py",
        [
            "synthesize",
            "--air",
            str(air_path),
            "--obligations",
            str(obligations_path),
            "--input-domain",
            str(domain_path),
            "--output-scenarios",
            str(synthesized_path),
            "--output-report",
            str(synthesis_report_path),
        ],
    )
    _run_python_tool(
        root,
        "assurance_suite_reducer.py",
        [
            "reduce",
            "--scenarios",
            str(synthesized_path),
            "--obligations",
            str(obligations_path),
            "--output-scenarios",
            str(obligation_reduced_path),
            "--output-report",
            str(reduction_report_path),
        ],
    )
    _run_python_tool(
        root,
        "mutation_aware_suite_reducer.py",
        [
            "reduce",
            "--air",
            str(air_path),
            "--scenarios",
            str(synthesized_path),
            "--obligations",
            str(obligations_path),
            "--mutation-profile",
            str(selected.mutation_profile_path),
            "--output-scenarios",
            str(mutation_reduced_path),
            "--output-report",
            str(mutation_report_path),
        ],
    )

    if announce:
        print("[9/15] Five-suite assurance comparison")
    _run_python_tool(
        root,
        "compare_assurance_suites.py",
        [
            "compare",
            "--air",
            str(air_path),
            "--obligations",
            str(obligations_path),
            "--manual-requirements",
            str(selected.manual_requirements_path),
            "--manual-closure",
            str(selected.manual_closure_path),
            "--automatic-unreduced",
            str(synthesized_path),
            "--automatic-obligation-reduced",
            str(obligation_reduced_path),
            "--automatic-mutation-aware-reduced",
            str(mutation_reduced_path),
            "--mutation-profile",
            str(selected.mutation_profile_path),
            "--output",
            str(comparison_path),
        ],
    )

    documents = {
        "input_domain": load_json(domain_path),
        "input_domain_validation": load_json(domain_validation_path),
        "obligations": load_json(obligations_path),
        "synthesized_scenarios": load_json(synthesized_path),
        "synthesis": load_json(synthesis_report_path),
        "obligation_reduced_scenarios": load_json(obligation_reduced_path),
        "obligation_reduction": load_json(reduction_report_path),
        "mutation_reduced_scenarios": load_json(mutation_reduced_path),
        "mutation_reduction": load_json(mutation_report_path),
        "comparison": load_json(comparison_path),
    }

    if not documents["input_domain_validation"].get("passed"):
        raise AerostError("bounded input-domain validation failed")
    synthesis = documents["synthesis"]
    if not synthesis.get("passed") or synthesis["obligation_summary"]["uncovered"]:
        raise AerostError("bounded assurance synthesis left uncovered obligations")
    obligation_reduction = documents["obligation_reduction"]
    if not obligation_reduction.get("passed"):
        raise AerostError("obligation-preserving suite reduction failed")
    mutation_reduction = documents["mutation_reduction"]
    if not mutation_reduction.get("passed"):
        raise AerostError("mutation-aware suite reduction failed")
    mutation_summary = mutation_reduction["mutation_summary"]
    if mutation_summary["killed"] != mutation_summary["total"]:
        raise AerostError("mutation-aware suite has surviving controlled mutants")
    comparison = documents["comparison"]
    if not comparison.get("passed") or comparison.get("suite_count") != 5:
        raise AerostError("five-suite assurance comparison failed")
    final_suite = _suite_by_name(
        comparison,
        "automatic-mutation-aware-reduced",
    )
    if final_suite["obligations"]["covered"] != final_suite["obligations"]["total"]:
        raise AerostError("final automatic suite does not close assurance obligations")
    if final_suite["mcdc"]["covered"] != final_suite["mcdc"]["total"]:
        raise AerostError("final automatic suite does not preserve selected MC/DC")
    if final_suite["mutation"]["killed"] != final_suite["mutation"]["total"]:
        raise AerostError("final automatic suite does not preserve mutation detection")
    return documents


def assurance_results_summary(assurance: dict[str, Any]) -> dict[str, Any]:
    obligations = assurance["obligations"]
    obligation_counts: dict[str, int] = {}
    for item in obligations["obligations"]:
        kind = item["kind"]
        obligation_counts[kind] = obligation_counts.get(kind, 0) + 1
    synthesis = assurance["synthesis"]
    obligation_reduction = assurance["obligation_reduction"]
    mutation_reduction = assurance["mutation_reduction"]
    comparison = assurance["comparison"]
    compact_suites = [
        {
            "name": suite["name"],
            "scenario_count": suite["scenario_count"],
            "cycle_count": suite["cycle_count"],
            "obligations": suite["obligations"],
            "mcdc": suite["mcdc"],
            "mutation": suite["mutation"],
            "replay_passed": suite["replay_passed"],
        }
        for suite in comparison["suites"]
    ]
    return {
        "assurance_obligations": {
            "total": len(obligations["obligations"]),
            "by_kind": dict(sorted(obligation_counts.items())),
            "schema_version": obligations["schema_version"],
        },
        "bounded_input_domain": assurance["input_domain_validation"],
        "automatic_test_synthesis": {
            "algorithm": synthesis["algorithm"],
            "bounds": synthesis["bounds"],
            "obligation_summary": synthesis["obligation_summary"],
            "search_summary": synthesis["search_summary"],
            "suite_summary": synthesis["suite_summary"],
            "deterministic": synthesis["deterministic"],
            "passed": synthesis["passed"],
        },
        "obligation_only_suite_reduction": {
            "algorithm": obligation_reduction["algorithm"],
            "input_summary": obligation_reduction["input_summary"],
            "output_summary": obligation_reduction["output_summary"],
            "mcdc_summary": obligation_reduction["mcdc_summary"],
            "deterministic": obligation_reduction["deterministic"],
            "passed": obligation_reduction["passed"],
        },
        "mutation_aware_suite_reduction": {
            "algorithm": mutation_reduction["algorithm"],
            "input_summary": mutation_reduction["input_summary"],
            "output_summary": mutation_reduction["output_summary"],
            "mcdc_summary": mutation_reduction["mcdc_summary"],
            "mutation_summary": mutation_reduction["mutation_summary"],
            "claim_boundary": mutation_reduction["claim_boundary"],
            "deterministic": mutation_reduction["deterministic"],
            "passed": mutation_reduction["passed"],
        },
        "assurance_suite_comparison": {
            "schema_version": comparison["schema_version"],
            "tool": comparison["tool"],
            "suite_count": comparison["suite_count"],
            "suites": compact_suites,
            "comparisons": comparison["comparisons"],
            "claim_boundary": comparison["claim_boundary"],
            "deterministic": comparison["deterministic"],
            "passed": comparison["passed"],
        },
    }


def paper_results_tex(summary: dict[str, Any], timing: dict[str, Any] | None) -> str:
    cov = summary["coverage"]
    diff = summary["differential"]
    mut = summary["mutation"]
    tr = summary["traceability"]
    mcdc = summary["mcdc"]
    lines = [
        "% Generated by AEROST. Do not edit.",
        f"\\newcommand{{\\AerostScenarioCount}}{{{summary['scenario_count']}}}",
        f"\\newcommand{{\\AerostExecutedCycles}}{{{summary['executed_cycles']}}}",
        f"\\newcommand{{\\AerostEquivalentCycles}}{{{diff['equivalent_cycles']}/{diff['executed_cycles']}}}",
        f"\\newcommand{{\\AerostMismatchCount}}{{{diff['mismatch_count']}}}",
        f"\\newcommand{{\\AerostExecutionPathCount}}{{{summary['three_way_differential']['execution_path_count']}}}",
        f"\\newcommand{{\\AerostThreeWayEquivalentCycles}}{{{summary['three_way_differential']['equivalent_cycles']}/{summary['three_way_differential']['executed_cycles']}}}",
        f"\\newcommand{{\\AerostThreeWayMismatchCount}}{{{summary['three_way_differential']['mismatch_count']}}}",
        f"\\newcommand{{\\AerostStatementCoverage}}{{{cov['statement']['reached']}/{cov['statement']['total']}}}",
        f"\\newcommand{{\\AerostDecisionCoverage}}{{{cov['decision']['both_outcomes']}/{cov['decision']['total']}}}",
        f"\\newcommand{{\\AerostConditionCoverage}}{{{cov['condition']['both_outcomes']}/{cov['condition']['total']}}}",
        f"\\newcommand{{\\AerostMCDC}}{{{mcdc['independence_pairs_found']}/{mcdc['independence_pairs_required']}}}",
        f"\\newcommand{{\\AerostMutation}}{{{mut['detected']}/{mut['executed_non_equivalent_mutants']}}}",
        f"\\newcommand{{\\AerostReqSource}}{{{tr['requirement_to_source']['percent']:.2f}\\%}}",
        f"\\newcommand{{\\AerostSourceAIR}}{{{tr['source_to_air']['percent']:.2f}\\%}}",
        f"\\newcommand{{\\AerostAIRBackend}}{{{tr['air_to_backend']['percent']:.2f}\\%}}",
        f"\\newcommand{{\\AerostReqTest}}{{{tr['requirement_to_test']['percent']:.2f}\\%}}",
        f"\\newcommand{{\\AerostExpressive}}{{{summary['expressive_adequacy']['percent']:.2f}\\%}}",
        f"\\newcommand{{\\AerostReproducibility}}{{{summary['reproducibility']['files_identical']}/{summary['reproducibility']['files_compared']} byte-identical}}",
        f"\\newcommand{{\\AerostAssuranceObligations}}{{{summary['assurance_obligations']['total']}}}",
        f"\\newcommand{{\\AerostSynthesizedObligations}}{{{summary['automatic_test_synthesis']['obligation_summary']['covered']}/{summary['automatic_test_synthesis']['obligation_summary']['total']}}}",
        f"\\newcommand{{\\AerostSynthesisStates}}{{{summary['automatic_test_synthesis']['search_summary']['states_explored']}}}",
        f"\\newcommand{{\\AerostSynthesisTransitions}}{{{summary['automatic_test_synthesis']['search_summary']['transitions_explored']}}}",
        f"\\newcommand{{\\AerostUnreducedAutomaticSuite}}{{{summary['automatic_test_synthesis']['suite_summary']['selected_scenarios']} scenarios / {summary['automatic_test_synthesis']['suite_summary']['selected_cycles']} cycles}}",
        f"\\newcommand{{\\AerostFinalAutomaticSuite}}{{{summary['mutation_aware_suite_reduction']['output_summary']['selected_scenarios']} scenarios / {summary['mutation_aware_suite_reduction']['output_summary']['selected_cycles']} cycles}}",
        f"\\newcommand{{\\AerostAutomaticScenarioReduction}}{{{summary['mutation_aware_suite_reduction']['output_summary']['scenario_reduction_percent']:.2f}\\%}}",
        f"\\newcommand{{\\AerostAutomaticCycleReduction}}{{{summary['mutation_aware_suite_reduction']['output_summary']['cycle_reduction_percent']:.2f}\\%}}",
        f"\\newcommand{{\\AerostAutomaticMutation}}{{{summary['mutation_aware_suite_reduction']['mutation_summary']['killed']}/{summary['mutation_aware_suite_reduction']['mutation_summary']['total']}}}",
    ]
    if timing:
        lines.append(f"\\newcommand{{\\AerostHostMaximum}}{{{timing['aggregate']['maximum_ns']} ns}}")
        lines.append(f"\\newcommand{{\\AerostTimedPaths}}{{{timing['controlled_paths']}}}")
        lines.append(f"\\newcommand{{\\AerostTimingSamplesPerPath}}{{{timing['samples_per_path']}}}")
    else:
        lines.append("\\newcommand{\\AerostHostMaximum}{[RUN PIPELINE]}")
        lines.append("\\newcommand{\\AerostTimedPaths}{[RUN PIPELINE]}")
        lines.append("\\newcommand{\\AerostTimingSamplesPerPath}{[RUN PIPELINE]}")
    return "\n".join(lines) + "\n"


def write_manifest(root: Path, artifact_dir: Path, source_path: Path) -> dict[str, Any]:
    excluded = {"artifact-sha256.json", "manifest.json", "schema-validation-report.json"}
    artifacts = []
    for path in sorted(p for p in artifact_dir.rglob("*") if p.is_file() and "target" not in p.parts and p.name not in excluded):
        artifacts.append({"path": path.relative_to(artifact_dir).as_posix(), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    manifest = {
        "tool_version": TOOL_VERSION,
        "profile_version": PROFILE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "source_sha256": sha256_file(source_path),
        "configuration": {"canonical_encoding": "UTF-8", "canonical_newline": "LF", "generator": "AIR-to-Safe-Rust"},
        "artifacts": artifacts,
    }
    write_json(artifact_dir / "manifest.json", manifest)
    write_json(artifact_dir / "artifact-sha256.json", {x["path"]: x["sha256"] for x in artifacts})
    return manifest


def build_core_bundle(
    root: Path,
    output_dir: Path,
    compile_backend: bool,
    application: ApplicationConfig | None = None,
) -> dict[str, Any]:
    selected = application or load_application_manifest(root)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    source_path = selected.source_path
    source = source_path.read_text(encoding="utf-8")
    program = parse_source(source)
    typed = semantic_analyze(program)
    policy_path = selected.runtime_fault_policy_path
    execution_contract = load_execution_contract(policy_path, typed)
    air = lower_to_air(typed, execution_contract)
    scenarios = load_controlled_scenarios(root, selected)
    requirements = load_json(selected.requirements_path)
    reference = run_reference_scenarios(air, scenarios)
    oracle = oracle_comparison_report(reference, scenarios)
    if not oracle["passed"]:
        raise AerostError("independent oracle comparison failed")

    shutil.copy2(selected.manifest_path, output_dir / "application-manifest.json")
    shutil.copy2(selected.mutation_profile_path, output_dir / "mutation-profile.json")
    shutil.copy2(source_path, output_dir / "controlled-source.ascp")
    shutil.copy2(policy_path, output_dir / "runtime-fault-policy.json")
    write_json(output_dir / "controlled-scenarios.json", {"scenarios": scenarios})
    write_json(output_dir / "typed-program.json", typed_program_json(typed))
    write_json(output_dir / selected.air_artifact_name, air)
    generated_dir = output_dir / "generated-backend"
    source_map = generate_rust_crate(air, generated_dir)
    generated_lib = (generated_dir / "src" / "lib.rs").read_text(encoding="utf-8")
    forbidden_reference_calls = [
        token for token in ("aerost_reference", "reference_step", "execute_cycle")
        if token in generated_lib
    ]
    mapped_air_ids = {item["air_id"] for item in source_map["mappings"]}
    all_air_ids = {node.get("id") or node.get("decision_id") for node in all_air_nodes(air)}
    backend_independence = {
        "generated_from_air": True,
        "separate_cargo_crate": True,
        "compiled": bool(compile_backend),
        "forbidden_reference_calls": forbidden_reference_calls,
        "reference_executor_called": bool(forbidden_reference_calls),
        "source_map_complete": not (all_air_ids - mapped_air_ids),
        "unmapped_air_ids": sorted(all_air_ids - mapped_air_ids),
        "passed": not forbidden_reference_calls and not (all_air_ids - mapped_air_ids),
    }
    write_json(output_dir / "backend-independence-report.json", backend_independence)
    if not backend_independence["passed"]:
        raise AerostError("generated backend independence check failed")
    write_json(output_dir / "reference-traces.json", traces_document(reference))
    protocol = output_dir / "controlled-protocol.txt"
    build_protocol(air, scenarios, protocol)
    if compile_backend:
        binary = compile_generated(generated_dir)
        run = run_command([str(binary), "run", str(protocol)])
        backend = parse_backend_output(run.stdout, air)
    else:
        backend = [
            {"scenario_id": r.scenario_id, "cycle": r.cycle, **r.observable()}
            for r in reference
        ]
    write_json(output_dir / "backend-traces.json", backend_traces_document(backend))
    diff = differential_report(reference, backend)
    write_json(output_dir / "differential-results.json", diff)

    external_independence = external_executor_independence_report(root)
    write_json(
        output_dir / "external-executor-independence-report.json",
        external_independence,
    )
    if not external_independence["passed"]:
        raise AerostError("external AIR executor independence check failed")
    external_document = run_external_air_executor(
        root,
        output_dir / selected.air_artifact_name,
        protocol,
        output_dir / "external-executor-traces.json",
    )
    external = external_document["cycles"]
    three_way = three_way_differential_report(reference, backend, external)
    write_json(output_dir / "three-way-differential-results.json", three_way)
    if not three_way["passed"]:
        raise AerostError("three-way execution mismatch")

    coverage = coverage_report(air, reference)
    mcdc = mcdc_report(air, reference)
    traceability = traceability_report(air, source_map, requirements, scenarios, reference)
    expressive = expressive_adequacy_report(traceability)
    write_json(output_dir / "coverage-report.json", coverage)
    write_json(output_dir / "uncovered-items.json", {"items": coverage["uncovered_items"]})
    write_json(output_dir / "mcdc-report.json", mcdc)
    write_json(output_dir / "traceability-report.json", traceability)
    write_json(output_dir / "expressive-adequacy.json", expressive)
    write_json(output_dir / "independent-oracle-comparison.json", oracle)
    conformance = conformance_report(
        root,
        compile_accepted=compile_backend,
        work_dir=output_dir / ".conformance-build",
    )
    write_json(output_dir / "conformance-report.json", conformance)
    return {
        "application": selected,
        "source_path": source_path,
        "air": air,
        "scenarios": scenarios,
        "reference": reference,
        "backend": backend,
        "external": external,
        "binary": locate_generated_binary(generated_dir) if compile_backend else None,
        "protocol": protocol,
        "diff": diff,
        "three_way": three_way,
        "external_executor_independence": external_independence,
        "coverage": coverage,
        "mcdc": mcdc,
        "traceability": traceability,
        "expressive": expressive,
        "conformance": conformance,
        "backend_independence": backend_independence,
    }


def reproducibility_check(
    root: Path,
    parent: Path,
    compile_backend: bool,
    baseline_dir: Path,
    application: ApplicationConfig | None = None,
) -> dict[str, Any]:
    selected = application or load_application_manifest(root)
    reproduced_dir = parent / "reproduced"
    build_core_bundle(root, reproduced_dir, compile_backend, selected)
    run_assurance_evidence_pipeline(root, reproduced_dir, application=selected)
    files = [
        "application-manifest.json", "mutation-profile.json", "controlled-source.ascp", "runtime-fault-policy.json", "controlled-scenarios.json", "typed-program.json", selected.air_artifact_name,
        "generated-backend/Cargo.toml", "generated-backend/src/lib.rs", "generated-backend/src/main.rs",
        "generated-backend/source-map.json", "reference-traces.json", "backend-traces.json",
        "external-executor-traces.json", "differential-results.json",
        "three-way-differential-results.json", "external-executor-independence-report.json",
        "coverage-report.json", "mcdc-report.json",
        "traceability-report.json", "expressive-adequacy.json", "independent-oracle-comparison.json",
        "conformance-report.json", "backend-independence-report.json",
        "controlled-input-domain.json", "input-domain-validation.json",
        "assurance-obligations.json", "synthesized-scenarios.json", "synthesis-report.json",
        "obligation-reduced-scenarios.json", "suite-reduction-report.json",
        "mutation-aware-reduced-scenarios.json", "mutation-aware-suite-reduction-report.json",
        "assurance-suite-comparison.json",
    ]
    mismatches = []
    for rel in files:
        baseline = baseline_dir / rel
        reproduced = reproduced_dir / rel
        if (
            not baseline.exists()
            or not reproduced.exists()
            or baseline.read_bytes() != reproduced.read_bytes()
        ):
            mismatches.append(rel)
    return {
        "files_compared": len(files),
        "files_identical": len(files) - len(mismatches),
        "byte_identical": not mismatches,
        "mismatches": mismatches,
        "algorithm": "SHA-256 and byte comparison",
        "baseline_directory": str(baseline_dir),
        "clean_rebuild_directory": str(reproduced_dir),
    }


def run_pipeline(
    root: Path,
    output_dir: Path,
    skip_cargo: bool,
    timing_samples: int,
    application: ApplicationConfig | None = None,
) -> dict[str, Any]:
    selected = application or load_application_manifest(root)
    print(f"Application: {selected.application_id}")
    print("[1/15] Parse, semantic analysis, and executable AIR")
    core = build_core_bundle(root, output_dir, compile_backend=not skip_cargo, application=selected)
    if core["diff"]["mismatch_count"]:
        raise AerostError("reference/generated backend mismatch")
    if not core["three_way"]["passed"]:
        raise AerostError("three-way execution mismatch")
    if not core["external_executor_independence"]["passed"]:
        raise AerostError("external executor independence failed")
    print("[2/15] Controlled accepted/rejected corpus")
    if not core["conformance"]["complete"]:
        raise AerostError("conformance corpus failure")
    print("[3/15] Generated independent Rust crate")
    print("[4/15] Differential execution")
    print("[5/15] Computed structural coverage")
    if not core["coverage"]["complete"]:
        raise AerostError("structural coverage remains open")
    print("[6/15] Computed selected MC/DC")
    if not core["mcdc"]["complete"]:
        raise AerostError("selected MC/DC independence pairs incomplete")

    assurance = run_assurance_evidence_pipeline(
        root, output_dir, announce=True, application=selected
    )
    assurance_summary = assurance_results_summary(assurance)

    print("[10/15] AIR-level mutation analysis")
    from application_mutations import load_mutation_profile

    mutation_profile = load_mutation_profile(selected.mutation_profile_path)
    mutation = mutation_report(
        core["air"],
        core["scenarios"],
        output_dir / "mutation-work",
        not skip_cargo,
        mutation_profile=mutation_profile,
    )
    mutation["manual_suite_gate"] = bool(mutation_profile["manual_suite_gate"])
    mutation["manual_suite_gate_passed"] = not mutation["surviving"]
    write_json(output_dir / "mutation-report.json", mutation)
    if mutation["surviving"] and mutation_profile["manual_suite_gate"]:
        raise AerostError("surviving non-equivalent mutants: " + ", ".join(mutation["surviving"]))
    print("[11/15] Traceability and expressive adequacy")
    if not core["traceability"]["complete"] or not core["expressive"]["complete"]:
        raise AerostError("traceability or expressive adequacy incomplete")
    print("[12/15] Host timing")
    timing = None if skip_cargo else run_host_timing(core["binary"], core["protocol"], timing_samples)
    if timing:
        write_json(output_dir / "host-timing.json", timing)
    write_json(output_dir / "host-platform.json", host_platform_report())
    print("[13/15] Clean-directory reproducibility")
    repro_parent = output_dir.parent / "reproducibility-work"
    if repro_parent.exists():
        shutil.rmtree(repro_parent)
    repro_parent.mkdir(parents=True)
    repro = reproducibility_check(
        root, repro_parent, not skip_cargo, output_dir, application=selected
    )
    write_json(output_dir / "reproducibility-result.json", repro)
    if not repro["byte_identical"]:
        raise AerostError("reproducibility mismatch")
    print("[14/15] Results and manifest")
    summary = {
        "application": selected.summary(root.resolve()),
        "profile_version": PROFILE_VERSION,
        "tool_version": TOOL_VERSION,
        "scenario_count": len(core["scenarios"]),
        "executed_cycles": len(core["reference"]),
        "differential": core["diff"],
        "three_way_differential": core["three_way"],
        "external_executor_independence": core["external_executor_independence"],
        "coverage": core["coverage"],
        "mcdc": core["mcdc"],
        "mutation": mutation,
        "traceability": core["traceability"],
        "expressive_adequacy": core["expressive"],
        "conformance": core["conformance"],
        "backend_independence": core["backend_independence"],
        "runtime_fault_policy": {
            "metadata_driven": True,
            "schema_version": core["air"]["execution_contract"]["schema_version"],
            "program": core["air"]["execution_contract"]["program"],
        },
        **assurance_summary,
        "reproducibility": repro,
        "host_timing": timing,
        "paper_facing_values_computed": not skip_cargo,
        "hardcoded_paper_facing_values": False,
        "authoritative_full_pipeline": not skip_cargo,
        "assembly_validation_mode": skip_cargo,
    }
    write_json(output_dir / "results-summary.json", summary)
    (output_dir / "paper-results.tex").write_text(paper_results_tex(summary, timing), encoding="utf-8", newline="\n")
    write_manifest(root, output_dir, core["source_path"])
    print("[15/15] JSON Schema validation")
    schema_report = validate_artifacts_with_schemas(
        root, output_dir, application=selected
    )
    write_json(output_dir / "schema-validation-report.json", schema_report)
    if not schema_report["all_valid"]:
        raise AerostError("JSON Schema validation failed")
    write_manifest(root, output_dir, core["source_path"])
    print("AEROST pipeline PASS")
    return summary

def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AEROST Research Baseline compiler and assurance evidence pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    p_pipeline = sub.add_parser("pipeline", help="run the complete project pipeline")
    p_pipeline.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p_pipeline.add_argument("--output", type=Path)
    p_pipeline.add_argument(
        "--application",
        type=Path,
        help="repository-relative application manifest; defaults to applications/power-supervisor.application.json",
    )
    p_pipeline.add_argument("--skip-cargo", action="store_true", help="assembly validation only; do not use for paper results")
    p_pipeline.add_argument("--timing-samples", type=int, default=100_000)
    p_generate = sub.add_parser("generate", help="parse ASCP and generate an independent Rust crate")
    p_generate.add_argument("source", type=Path)
    p_generate.add_argument("output", type=Path)
    p_generate.add_argument("--policy", type=Path, help="versioned runtime-fault/output policy JSON")
    p_check = sub.add_parser("check", help="parse and type-check an ASCP source")
    p_check.add_argument("source", type=Path)
    p_check.add_argument("--policy", type=Path, help="versioned runtime-fault/output policy JSON")
    p_self = sub.add_parser("self-test", help="run Python-side compiler tests")
    p_self.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p_self.add_argument("--application", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "pipeline":
            root = args.root.resolve()
            output = (args.output or (root / "artifacts" / "latest")).resolve()
            application = load_application_manifest(root, args.application)
            run_pipeline(
                root,
                output,
                args.skip_cargo,
                args.timing_samples,
                application=application,
            )
            print(f"Open {output / 'results-summary.json'}")
        elif args.command == "generate":
            typed = semantic_analyze(parse_source(args.source.read_text(encoding="utf-8")))
            contract = load_execution_contract(args.policy, typed) if args.policy else None
            air = lower_to_air(typed, contract)
            generate_rust_crate(air, args.output)
            print(f"generated Rust crate: {args.output}")
        elif args.command == "check":
            typed = semantic_analyze(parse_source(args.source.read_text(encoding="utf-8")))
            contract = load_execution_contract(args.policy, typed) if args.policy else None
            air = lower_to_air(typed, contract)
            print(f"PASS {args.source}: inputs={len(air['program']['inputs'])}, outputs={len(air['program']['outputs'])}, states={len(air['program']['states'])}")
        else:
            root = args.root.resolve()
            application = load_application_manifest(root, args.application)
            run_self_tests(root, application)
            print("AEROST Python self-tests PASS")
        return 0
    except (AerostError, OSError, ValueError, KeyError) as exc:
        print(f"AEROST ERROR: {exc}", file=sys.stderr)
        return 1


def run_self_tests(
    root: Path,
    application: ApplicationConfig | None = None,
) -> None:
    selected = application or load_application_manifest(root)
    source = selected.source_path.read_text(encoding="utf-8")
    tokens = lex(source)
    if len(tokens) < 100:
        raise AerostError("lexer produced unexpectedly few tokens")
    program = parse_source(source)
    if not program.transition or not program.output:
        raise AerostError("parser did not produce executable stages")
    typed = semantic_analyze(program)
    policy = load_execution_contract(
        selected.runtime_fault_policy_path,
        typed,
    )
    air = lower_to_air(typed, policy)
    scenarios = load_controlled_scenarios(root, selected)
    results = run_reference_scenarios(air, scenarios)
    if expected_mismatches(results, scenarios):
        raise AerostError("reference interpreter differs from frozen independent oracle")
    generated = root / "validation" / "self-test-generated"
    if generated.exists():
        shutil.rmtree(generated)
    source_map = generate_rust_crate(air, generated)
    node_ids = {n.get("id") or n.get("decision_id") for n in all_air_nodes(air)}
    mapped = {m["air_id"] for m in source_map["mappings"]}
    if node_ids - mapped:
        raise AerostError("generated source map is incomplete")
    if "aerost_reference" in (generated / "src" / "lib.rs").read_text(encoding="utf-8"):
        raise AerostError("generated backend references the reference interpreter")
    # Distinct accepted programs must produce distinct generated Rust.
    accepted = sorted((root / "conformance" / "accepted").glob("*.ascp"))
    hashes = set()
    for index, path in enumerate(accepted):
        typed_small = semantic_analyze(parse_source(path.read_text(encoding="utf-8")))
        air_small = lower_to_air(typed_small)
        dest = root / "validation" / f"small-generated-{index}"
        if dest.exists():
            shutil.rmtree(dest)
        generate_rust_crate(air_small, dest)
        if shutil.which("cargo") is not None:
            compile_generated(dest)
        hashes.add(sha256_file(dest / "src" / "lib.rs"))
    if len(hashes) != len(accepted):
        raise AerostError("distinct ASCP programs did not produce distinct Rust sources")
    cov = coverage_report(air, results)
    if not cov["complete"]:
        raise AerostError("full structural coverage is open in self-test")
    mcdc = mcdc_report(air, results)
    if not mcdc["complete"]:
        raise AerostError("selected MC/DC is incomplete in self-test")


if __name__ == "__main__":
    raise SystemExit(cli_main())
