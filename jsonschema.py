"""
An implementation of JSON Schema for Python

The main functionality is provided by the validator classes for each of the
supported JSON Schema versions.

Most commonly, :func:`validate` is the quickest way to simply validate a given
instance under a schema, and will create a validator for you.

"""

from __future__ import division, unicode_literals

import collections
import contextlib
import datetime
import itertools
import json
import numbers
import operator
import re
import socket
import sys

try:
    import requests
except ImportError:
    requests = None

__version__ = "1.0.0-dev"

PY3 = sys.version_info[0] >= 3

if PY3:
    from urllib import parse as urlparse
    from urllib.parse import unquote
    from urllib.request import urlopen
    basestring = unicode = str
    long = int
    iteritems = operator.methodcaller("items")
else:
    from itertools import izip as zip
    from urllib import unquote
    from urllib2 import urlopen
    import urlparse
    iteritems = operator.methodcaller("iteritems")


FLOAT_TOLERANCE = 10 ** -15
validators = {}
meta_schemas = {}


class _Error(Exception):
    def __init__(self, message, validator=None, path=()):
        super(_Error, self).__init__(message, validator, path)
        self.message = message
        self.path = list(path)
        self.validator = validator

    def __str__(self):
        return self.message


class SchemaError(_Error): pass
class ValidationError(_Error): pass
class RefResolutionError(Exception): pass
class UnknownType(Exception): pass


def validates(version):
    """
    Register the decorated validator for a ``version`` of the specification.

    Registered validators and their meta schemas will be considered when
    parsing ``$schema`` properties' URIs.

    :argument str version: an identifier to use as the version's name
    :returns: a class decorator to decorate the validator with the version

    """

    def _validates(cls):
        validators[version] = cls
        if "id" in cls.META_SCHEMA:
            meta_schemas[cls.META_SCHEMA["id"]] = cls
        return cls
    return _validates


class ValidatorMixin(object):
    """
    Concrete implementation of :class:`IValidator`.

    Provides default implementations of each method. Validation of schema
    properties is dispatched to ``validate_property`` methods. E.g., to
    implement a validator for a ``maximum`` property, create a
    ``validate_maximum`` method. Validator methods should yield zero or more
    :exc:`ValidationError``\s to signal failed validation.

    """

    DEFAULT_TYPES = {
        "array" : list, "boolean" : bool, "integer" : (int, long),
        "null" : type(None), "number" : numbers.Number, "object" : dict,
        "string" : basestring,
    }

    def __init__(self, schema, types=(), resolver=None, format_checker=None):
        self._types = dict(self.DEFAULT_TYPES)
        self._types.update(types)

        if resolver is None:
            resolver = RefResolver.from_schema(schema)

        self.resolver = resolver
        self.format_checker = format_checker
        self.schema = schema

    def is_type(self, instance, type):
        if type not in self._types:
            raise UnknownType(type)
        pytypes = self._types[type]

        # bool inherits from int, so ensure bools aren't reported as integers
        if isinstance(instance, bool):
            pytypes = _flatten(pytypes)
            num = any(issubclass(pytype, numbers.Number) for pytype in pytypes)
            if num and bool not in pytypes:
                return False
        return isinstance(instance, pytypes)

    def is_valid(self, instance, _schema=None):
        error = next(self.iter_errors(instance, _schema), None)
        return error is None

    @classmethod
    def check_schema(cls, schema):
        for error in cls(cls.META_SCHEMA).iter_errors(schema):
            raise SchemaError(
                error.message, validator=error.validator, path=error.path,
            )

    def iter_errors(self, instance, _schema=None):
        if _schema is None:
            _schema = self.schema

        with self.resolver.in_scope(_schema.get("id", "")):
            for k, v in iteritems(_schema):
                validator_attr = "validate_%s" % (k.lstrip("$"),)
                validator = getattr(self, validator_attr, None)

                if validator is None:
                    continue

                errors = validator(v, instance, _schema) or ()
                for error in errors:
                    # set the validator if it wasn't already set by the
                    # called function
                    if error.validator is None:
                        error.validator = k
                    yield error

    def validate(self, *args, **kwargs):
        for error in self.iter_errors(*args, **kwargs):
            raise error


class _Draft34CommonMixin(object):
    """
    Contains the validator methods common to both JSON schema drafts.

    """

    def validate_patternProperties(self, patternProperties, instance, schema):
        if not self.is_type(instance, "object"):
            return

        for pattern, subschema in iteritems(patternProperties):
            for k, v in iteritems(instance):
                if re.search(pattern, k):
                    for error in self.iter_errors(v, subschema):
                        yield error

    def validate_additionalProperties(self, aP, instance, schema):
        if not self.is_type(instance, "object"):
            return

        extras = set(_find_additional_properties(instance, schema))

        if self.is_type(aP, "object"):
            for extra in extras:
                for error in self.iter_errors(instance[extra], aP):
                    yield error
        elif not aP and extras:
            error = "Additional properties are not allowed (%s %s unexpected)"
            yield ValidationError(error % _extras_msg(extras))

    def validate_items(self, items, instance, schema):
        if not self.is_type(instance, "array"):
            return

        if self.is_type(items, "object"):
            for index, item in enumerate(instance):
                for error in self.iter_errors(item, items):
                    error.path.append(index)
                    yield error
        else:
            for (index, item), subschema in zip(enumerate(instance), items):
                for error in self.iter_errors(item, subschema):
                    error.path.append(index)
                    yield error

    def validate_additionalItems(self, aI, instance, schema):
        if (
            not self.is_type(instance, "array") or
            self.is_type(schema.get("items", {}), "object")
        ):
            return

        if self.is_type(aI, "object"):
            for item in instance[len(schema):]:
                for error in self.iter_errors(item, aI):
                    yield error
        elif not aI and len(instance) > len(schema.get("items", [])):
            error = "Additional items are not allowed (%s %s unexpected)"
            yield ValidationError(
                error % _extras_msg(instance[len(schema.get("items", [])):])
            )

    def validate_minimum(self, minimum, instance, schema):
        if not self.is_type(instance, "number"):
            return

        instance = float(instance)
        if schema.get("exclusiveMinimum", False):
            failed = instance <= minimum
            cmp = "less than or equal to"
        else:
            failed = instance < minimum
            cmp = "less than"

        if failed:
            yield ValidationError(
                "%r is %s the minimum of %r" % (instance, cmp, minimum)
            )

    def validate_maximum(self, maximum, instance, schema):
        if not self.is_type(instance, "number"):
            return

        instance = float(instance)
        if schema.get("exclusiveMaximum", False):
            failed = instance >= maximum
            cmp = "greater than or equal to"
        else:
            failed = instance > maximum
            cmp = "greater than"

        if failed:
            yield ValidationError(
                "%r is %s the maximum of %r" % (instance, cmp, maximum)
            )

    def _validate_multipleOf(self, dB, instance, schema):
        if not self.is_type(instance, "number"):
            return

        if isinstance(dB, float):
            mod = instance % dB
            failed = (mod > FLOAT_TOLERANCE) and (dB - mod) > FLOAT_TOLERANCE
        else:
            failed = instance % dB

        if failed:
            yield ValidationError(
                "%r is not a multiple of %r" % (instance, dB)
            )

    def validate_minItems(self, mI, instance, schema):
        if self.is_type(instance, "array") and len(instance) < mI:
            yield ValidationError("%r is too short" % (instance,))

    def validate_maxItems(self, mI, instance, schema):
        if self.is_type(instance, "array") and len(instance) > mI:
            yield ValidationError("%r is too long" % (instance,))

    def validate_uniqueItems(self, uI, instance, schema):
        if uI and self.is_type(instance, "array") and not _uniq(instance):
            yield ValidationError("%r has non-unique elements" % instance)

    def validate_pattern(self, patrn, instance, schema):
        if self.is_type(instance, "string") and not re.search(patrn, instance):
            yield ValidationError("%r does not match %r" % (instance, patrn))

    def validate_format(self, format, instance, schema):
        if (
            self.format_checker is not None and
            self.is_type(instance, "string") and
            not self.format_checker.conforms(instance, format)
        ):
            yield ValidationError("%r is not a %r" % (instance, format))

    def validate_minLength(self, mL, instance, schema):
        if self.is_type(instance, "string") and len(instance) < mL:
            yield ValidationError("%r is too short" % (instance,))

    def validate_maxLength(self, mL, instance, schema):
        if self.is_type(instance, "string") and len(instance) > mL:
            yield ValidationError("%r is too long" % (instance,))

    def validate_dependencies(self, dependencies, instance, schema):
        if not self.is_type(instance, "object"):
            return

        for property, dependency in iteritems(dependencies):
            if property not in instance:
                continue

            if self.is_type(dependency, "object"):
                for error in self.iter_errors(instance, dependency):
                    yield error
            else:
                dependencies = _list(dependency)
                for dependency in dependencies:
                    if dependency not in instance:
                        yield ValidationError(
                            "%r is a dependency of %r" % (dependency, property)
                        )

    def validate_enum(self, enums, instance, schema):
        if instance not in enums:
            yield ValidationError("%r is not one of %r" % (instance, enums))

    def validate_ref(self, ref, instance, schema):
        with self.resolver.resolving(ref) as resolved:
            for error in self.iter_errors(instance, resolved):
                yield error


@validates("draft3")
class Draft3Validator(ValidatorMixin, _Draft34CommonMixin, object):
    """
    A validator for JSON Schema draft 3.

    """

    def validate_type(self, types, instance, schema):
        types = _list(types)

        for type in types:
            if type == "any":
                return
            if self.is_type(type, "object"):
                if self.is_valid(instance, type):
                    return
            elif self.is_type(type, "string"):
                if self.is_type(instance, type):
                    return
        else:
            yield ValidationError(_types_msg(instance, types))

    def validate_properties(self, properties, instance, schema):
        if not self.is_type(instance, "object"):
            return

        for property, subschema in iteritems(properties):
            if property in instance:
                for error in self.iter_errors(instance[property], subschema):
                    error.path.append(property)
                    yield error
            elif subschema.get("required", False):
                yield ValidationError(
                    "%r is a required property" % (property,),
                    validator="required",
                    path=[property],
                )

    def validate_disallow(self, disallow, instance, schema):
        for disallowed in _list(disallow):
            if self.is_valid(instance, {"type" : [disallowed]}):
                yield ValidationError(
                    "%r is disallowed for %r" % (disallowed, instance)
                )

    def validate_extends(self, extends, instance, schema):
        if self.is_type(extends, "object"):
            extends = [extends]
        for subschema in extends:
            for error in self.iter_errors(instance, subschema):
                yield error

    validate_divisibleBy = _Draft34CommonMixin._validate_multipleOf

    META_SCHEMA = {
        "$schema" : "http://json-schema.org/draft-03/schema#",
        "id" : "http://json-schema.org/draft-03/schema#",
        "type" : "object",

        "properties" : {
            "type" : {
                "type" : ["string", "array"],
                "items" : {"type" : ["string", {"$ref" : "#"}]},
                "uniqueItems" : True,
                "default" : "any"
            },
            "properties" : {
                "type" : "object",
                "additionalProperties" : {"$ref" : "#", "type": "object"},
                "default" : {}
            },
            "patternProperties" : {
                "type" : "object",
                "additionalProperties" : {"$ref" : "#"},
                "default" : {}
            },
            "additionalProperties" : {
                "type" : [{"$ref" : "#"}, "boolean"], "default" : {}
            },
            "items" : {
                "type" : [{"$ref" : "#"}, "array"],
                "items" : {"$ref" : "#"},
                "default" : {}
            },
            "additionalItems" : {
                "type" : [{"$ref" : "#"}, "boolean"], "default" : {}
            },
            "required" : {"type" : "boolean", "default" : False},
            "dependencies" : {
                "type" : ["string", "array", "object"],
                "additionalProperties" : {
                    "type" : ["string", "array", {"$ref" : "#"}],
                    "items" : {"type" : "string"}
                },
                "default" : {}
            },
            "minimum" : {"type" : "number"},
            "maximum" : {"type" : "number"},
            "exclusiveMinimum" : {"type" : "boolean", "default" : False},
            "exclusiveMaximum" : {"type" : "boolean", "default" : False},
            "minItems" : {"type" : "integer", "minimum" : 0, "default" : 0},
            "maxItems" : {"type" : "integer", "minimum" : 0},
            "uniqueItems" : {"type" : "boolean", "default" : False},
            "pattern" : {"type" : "string", "format" : "regex"},
            "minLength" : {"type" : "integer", "minimum" : 0, "default" : 0},
            "maxLength" : {"type" : "integer"},
            "enum" : {"type" : "array", "minItems" : 1, "uniqueItems" : True},
            "default" : {"type" : "any"},
            "title" : {"type" : "string"},
            "description" : {"type" : "string"},
            "format" : {"type" : "string"},
            "maxDecimal" : {"type" : "number", "minimum" : 0},
            "divisibleBy" : {
                "type" : "number",
                "minimum" : 0,
                "exclusiveMinimum" : True,
                "default" : 1
            },
            "disallow" : {
                "type" : ["string", "array"],
                "items" : {"type" : ["string", {"$ref" : "#"}]},
                "uniqueItems" : True
            },
            "extends" : {
                "type" : [{"$ref" : "#"}, "array"],
                "items" : {"$ref" : "#"},
                "default" : {}
            },
            "id" : {"type" : "string", "format" : "uri"},
            "$ref" : {"type" : "string", "format" : "uri"},
            "$schema" : {"type" : "string", "format" : "uri"},
        },
        "dependencies" : {
            "exclusiveMinimum" : "minimum", "exclusiveMaximum" : "maximum"
        },
    }


@validates("draft4")
class Draft4Validator(ValidatorMixin, _Draft34CommonMixin, object):
    """
    A validator for JSON Schema draft 4.

    """

    def validate_type(self, types, instance, schema):
        types = _list(types)

        if not any(self.is_type(instance, type) for type in types):
            yield ValidationError(_types_msg(instance, types))

    def validate_properties(self, properties, instance, schema):
        if not self.is_type(instance, "object"):
            return

        for property, subschema in iteritems(properties):
            if property in instance:
                for error in self.iter_errors(instance[property], subschema):
                    error.path.append(property)
                    yield error

    def validate_required(self, required, instance, schema):
        if not self.is_type(instance, "object"):
            return
        for property in required:
            if property not in instance:
                yield ValidationError("%r is required property" % property)

    def validate_minProperties(self, mP, instance, schema):
        if self.is_type(instance, "object") and len(instance) < mP:
            yield ValidationError("%r is too short" % (instance,))

    def validate_maxProperties(self, mP, instance, schema):
        if not self.is_type(instance, "object"):
            return
        if self.is_type(instance, "object") and len(instance) > mP:
            yield ValidationError("%r is too short" % (instance,))

    def validate_allOf(self, allOf, instance, schema):
        for subschema in allOf:
            for error in self.iter_errors(instance, subschema):
                yield error

    def validate_oneOf(self, oneOf, instance, schema):
        subschemas = iter(oneOf)
        first_valid = next(
            (s for s in subschemas if self.is_valid(instance, s)), None,
        )

        if first_valid is None:
            yield ValidationError(
                "%r is not valid under any of the given schemas." % (instance,)
            )
        else:
            more_valid = [s for s in subschemas if self.is_valid(instance, s)]
            if more_valid:
                more_valid.append(first_valid)
                reprs = ", ".join(repr(schema) for schema in more_valid)
                yield ValidationError(
                    "%r is valid under each of %s" % (instance, reprs)
                )

    def validate_anyOf(self, anyOf, instance, schema):
        if not any(self.is_valid(instance, subschema) for subschema in anyOf):
            yield ValidationError(
                "The instance is not valid under any of the given schemas."
            )

    def validate_not(self, not_schema, instance, schema):
        if self.is_valid(instance, not_schema):
            yield ValidationError(
                "%r is not allowed for %r" % (not_schema, instance)
            )

    validate_multipleOf = _Draft34CommonMixin._validate_multipleOf

    META_SCHEMA = {
        "id": "http://json-schema.org/draft-04/schema#",
        "$schema": "http://json-schema.org/draft-04/schema#",
        "description": "Core schema meta-schema",
        "definitions": {
            "schemaArray": {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#"}
            },
            "positiveInteger": {
                "type": "integer",
                "minimum": 0
            },
            "positiveIntegerDefault0": {
                "allOf": [
                    {"$ref": "#/definitions/positiveInteger"}, {"default": 0}
                ]
            },
            "simpleTypes": {
                "enum": [
                    "array",
                    "boolean",
                    "integer",
                    "null",
                    "number",
                    "object",
                    "string",
                ]
            },
            "stringArray": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "uniqueItems": True
            }
        },
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "format": "uri"
            },
            "$schema": {
                "type": "string",
                "format": "uri"
            },
            "title": {
                "type": "string"
            },
            "description": {
                "type": "string"
            },
            "default": {},
            "multipleOf": {
                "type": "number",
                "minimum": 0,
                "exclusiveMinimum": True
            },
            "maximum": {
                "type": "number"
            },
            "exclusiveMaximum": {
                "type": "boolean",
                "default": False
            },
            "minimum": {
                "type": "number"
            },
            "exclusiveMinimum": {
                "type": "boolean",
                "default": False
            },
            "maxLength": {"$ref": "#/definitions/positiveInteger"},
            "minLength": {"$ref": "#/definitions/positiveIntegerDefault0"},
            "pattern": {
                "type": "string",
                "format": "regex"
            },
            "additionalItems": {
                "anyOf": [
                    {"type": "boolean"},
                    {"$ref": "#"}
                ],
                "default": {}
            },
            "items": {
                "anyOf": [
                    {"$ref": "#"},
                    {"$ref": "#/definitions/schemaArray"}
                ],
                "default": {}
            },
            "maxItems": {"$ref": "#/definitions/positiveInteger"},
            "minItems": {"$ref": "#/definitions/positiveIntegerDefault0"},
            "uniqueItems": {
                "type": "boolean",
                "default": False
            },
            "maxProperties": {"$ref": "#/definitions/positiveInteger"},
            "minProperties": {"$ref": "#/definitions/positiveIntegerDefault0"},
            "required": {"$ref": "#/definitions/stringArray"},
            "additionalProperties": {
                "anyOf": [
                    {"type": "boolean"},
                    {"$ref": "#"}
                ],
                "default": {}
            },
            "definitions": {
                "type": "object",
                "additionalProperties": {"$ref": "#"},
                "default": {}
            },
            "properties": {
                "type": "object",
                "additionalProperties": {"$ref": "#"},
                "default": {}
            },
            "patternProperties": {
                "type": "object",
                "additionalProperties": {"$ref": "#"},
                "default": {}
            },
            "dependencies": {
                "type": "object",
                "additionalProperties": {
                    "anyOf": [
                        {"$ref": "#"},
                        {"$ref": "#/definitions/stringArray"}
                    ]
                }
            },
            "enum": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True
            },
            "type": {
                "anyOf": [
                    {"$ref": "#/definitions/simpleTypes"},
                    {
                        "type": "array",
                        "items": {"$ref": "#/definitions/simpleTypes"},
                        "minItems": 1,
                        "uniqueItems": True
                    }
                ]
            },
            "allOf": {"$ref": "#/definitions/schemaArray"},
            "anyOf": {"$ref": "#/definitions/schemaArray"},
            "oneOf": {"$ref": "#/definitions/schemaArray"},
            "not": {"$ref": "#"}
        },
        "dependencies": {
            "exclusiveMaximum": ["maximum"],
            "exclusiveMinimum": ["minimum"]
        },
        "default": {}
    }


class FormatChecker(object):
    """
    A ``format`` property checker.

    JSON Schema does not mandate that the ``format`` property actually do any
    validation. If validation is desired however, instances of this class can
    be hooked into validators to enable format validation.

    :class:`FormatChecker` objects always return ``True`` when asked about
    formats that they do not know how to validate.

    To check a custom format using a function that takes an instance and
    returns a ``bool``, use the :meth:`FormatChecker.checks` or
    :meth:`FormatChecker.cls_checks` decorators.

    :argument iterable formats: the known formats to validate. This argument
                                can be used to limit which formats will be used
                                during validation.

        >>> checker = FormatChecker(formats=("date-time", "regex"))

    """

    checkers = {}

    def __init__(self, formats=None):
        if formats is None:
            self.checkers = self.checkers.copy()
        else:
            self.checkers = dict((k, self.checkers[k]) for k in formats)

    def checks(self, format):
        """
        Register a decorated function as validating a new format.

        :argument str format: the format that the decorated function will check

        """

        def _checks(func):
            self.checkers[format] = func
            return func
        return _checks

    cls_checks = classmethod(checks)

    def conforms(self, instance, format):
        """
        Check whether the instance conforms to the given format.

        :argument instance: the instance to check
        :type: any primitive type (str, number, bool)
        :argument str format: the format that instance should conform to
        :rtype: bool

        """

        if format in self.checkers:
            return self.checkers[format](instance)
        return True


@FormatChecker.cls_checks("email")
def is_email(instance):
    return "@" in instance


@FormatChecker.cls_checks("ipv4")
def is_ipv4(instance):
    try:
        socket.inet_aton(instance)
        return True
    except socket.error:
        return False


if hasattr(socket, "inet_pton"):
    @FormatChecker.cls_checks("ipv6")
    def is_ipv6(instance):
        try:
            socket.inet_pton(socket.AF_INET6, instance)
            return True
        except socket.error:
            return False


@FormatChecker.cls_checks("hostname")
def is_host_name(instance):
    pattern = "^[A-Za-z0-9][A-Za-z0-9\.\-]{1,255}$"
    if not re.match(pattern, instance):
        return False
    components = instance.split(".")
    for component in components:
        if len(component) > 63:
            return False
    return True


@FormatChecker.cls_checks("regex")
def is_regex(instance):
    try:
        re.compile(instance)
        return True
    except re.error:
        return False


try:
    import rfc3987
except ImportError:
    pass
else:
    @FormatChecker.cls_checks("uri")
    def is_uri(instance):
        try:
            rfc3987.parse(instance, rule="URI_reference")
        except ValueError:
            return False
        return True


try:
    import isodate
except ImportError:
    pass
else:
    @FormatChecker.cls_checks("date-time")
    def is_date_time(instance):
        try:
            isodate.parse_datetime(instance)
            return True
        except (ValueError, isodate.ISO8601Error):
            return False


draft4_format_checker = FormatChecker()
draft3_format_checker = FormatChecker()
draft3_format_checker.checks("ip-address")(is_ipv4)
draft3_format_checker.checks("host-name")(is_host_name)


@draft3_format_checker.checks("date")
def is_date(instance):
    try:
        datetime.datetime.strptime(instance, "%Y-%m-%d")
        return True
    except ValueError:
        return False


@draft3_format_checker.checks("time")
def is_time(instance):
    try:
        datetime.datetime.strptime(instance, "%H:%M:%S")
        return True
    except ValueError:
        return False


try:
    import webcolors
except ImportError:
    pass
else:
    def is_css_color_code(instance):
        try:
            webcolors.normalize_hex(instance)
        except (ValueError, TypeError):
            return False
        return True


    @draft3_format_checker.checks("color")
    def is_css21_color(instance):
        if instance.lower() in webcolors.css21_names_to_hex:
            return True
        return is_css_color_code(instance)


    def is_css3_color(instance):
        if instance.lower() in webcolors.css3_names_to_hex:
            return True
        return is_css_color_code(instance)


class RefResolver(object):
    """
    Resolve JSON References.

    :argument str base_uri: URI of the referring document
    :argument referrer: the actual referring document
    :argument dict store: a mapping from URIs to documents to cache
    :argument bool cache_remote: whether remote refs should be cached after
        first resolution
    :argument dict handlers: a mapping from URI schemes to functions that
        should be used to retrieve them

    """

    def __init__(self, base_uri, referrer, store=(), cache_remote=True,
                 handlers=()):
        self.base_uri = base_uri
        self.resolution_scope = base_uri
        self.referrer = referrer
        self.store = dict(store, **_meta_schemas())
        self.cache_remote = cache_remote
        self.handlers = dict(handlers)

    @classmethod
    def from_schema(cls, schema, *args, **kwargs):
        """
        Construct a resolver from a JSON schema object.

        :argument schema schema: the referring schema
        :rtype: :class:`RefResolver`

        """

        return cls(schema.get("id", ""), schema, *args, **kwargs)

    @contextlib.contextmanager
    def in_scope(self, scope):
        old_scope = self.resolution_scope
        self.resolution_scope = urlparse.urljoin(old_scope, scope)
        try:
            yield
        finally:
            self.resolution_scope = old_scope

    @contextlib.contextmanager
    def resolving(self, ref):
        """
        Context manager which resolves a JSON ``ref`` and enters the
        resolution scope of this ref.

        :argument str ref: reference to resolve

        """

        full_uri = urlparse.urljoin(self.resolution_scope, ref)
        uri, fragment = urlparse.urldefrag(full_uri)

        if uri in self.store:
            document = self.store[uri]
        elif not uri or uri == self.base_uri:
            document = self.referrer
        else:
            document = self.resolve_remote(uri)

        old_base_uri, old_referrer = self.base_uri, self.referrer
        self.base_uri, self.referrer = uri, document
        try:
            with self.in_scope(uri):
                yield self.resolve_fragment(document, fragment)
        finally:
            self.base_uri, self.referrer = old_base_uri, old_referrer

    def resolve_fragment(self, document, fragment):
        """
        Resolve a ``fragment`` within the referenced ``document``.

        :argument document: the referrant document
        :argument str fragment: a URI fragment to resolve within it

        """

        fragment = fragment.lstrip("/")
        parts = unquote(fragment).split("/") if fragment else []

        for part in parts:
            part = part.replace("~1", "/").replace("~0", "~")

            if part not in document:
                raise RefResolutionError(
                    "Unresolvable JSON pointer: %r" % fragment
                )

            document = document[part]

        return document

    def resolve_remote(self, uri):
        """
        Resolve a remote ``uri``.

        Does not check the store first.

        .. note::

            If the requests_ library is present, ``jsonschema`` will use it to
            request the remote ``uri``, so that the correct encoding is
            detected and used.

            If it isn't, or if the scheme of the ``uri`` is not ``http`` or
            ``https``, UTF-8 is assumed.

        :argument str uri: the URI to resolve
        :returns: the retrieved document

        .. _requests: http://pypi.python.org/pypi/requests/

        """

        scheme = urlparse.urlsplit(uri).scheme

        if scheme in self.handlers:
            result = self.handlers[scheme](uri)
        elif (
            scheme in ["http", "https"] and
            requests and
            getattr(requests.Response, "json", None) is not None
        ):
            # Requests has support for detecting the correct encoding of
            # json over http
            if callable(requests.Response.json):
                result = requests.get(uri).json()
            else:
                result = requests.get(uri).json
        else:
            # Otherwise, pass off to urllib and assume utf-8
            result = json.loads(urlopen(uri).read().decode("utf-8"))

        if self.cache_remote:
            self.store[uri] = result
        return result


class ErrorTree(object):
    """
    ErrorTrees make it easier to check which validations failed.

    """

    def __init__(self, errors=()):
        self.errors = {}
        self._contents = collections.defaultdict(self.__class__)

        for error in errors:
            container = self
            for element in reversed(error.path):
                container = container[element]
            container.errors[error.validator] = error

    def __contains__(self, k):
        return k in self._contents

    def __getitem__(self, k):
        """
        Retrieve the child tree with key ``k``.

        """

        return self._contents[k]

    def __setitem__(self, k, v):
        self._contents[k] = v

    def __iter__(self):
        return iter(self._contents)

    def __len__(self):
        return self.total_errors

    def __repr__(self):
        return "<%s (%s total errors)>" % (self.__class__.__name__, len(self))

    @property
    def total_errors(self):
        """
        The total number of errors in the entire tree, including children.

        """

        child_errors = sum(len(tree) for _, tree in iteritems(self._contents))
        return len(self.errors) + child_errors


def _meta_schemas():
    """
    Collect the urls and meta schemas from each known validator.

    """

    meta_schemas = (v.META_SCHEMA for v in validators.values())
    return dict((urlparse.urldefrag(m["id"])[0], m) for m in meta_schemas)


def _find_additional_properties(instance, schema):
    """
    Return the set of additional properties for the given ``instance``.

    Weeds out properties that should have been validated by ``properties`` and
    / or ``patternProperties``.

    Assumes ``instance`` is dict-like already.

    """

    properties = schema.get("properties", {})
    patterns = "|".join(schema.get("patternProperties", {}))
    for property in instance:
        if property not in properties:
            if patterns and re.search(patterns, property):
                continue
            yield property


def _extras_msg(extras):
    """
    Create an error message for extra items or properties.

    """

    if len(extras) == 1:
        verb = "was"
    else:
        verb = "were"
    return ", ".join(repr(extra) for extra in extras), verb


def _types_msg(instance, types):
    """
    Create an error message for a failure to match the given types.

    If the ``instance`` is an object and contains a ``name`` property, it will
    be considered to be a description of that object and used as its type.

    Otherwise the message is simply the reprs of the given ``types``.

    """

    reprs = []
    for type in types:
        try:
            reprs.append(repr(type["name"]))
        except Exception:
            reprs.append(repr(type))
    return "%r is not of type %s" % (instance, ", ".join(reprs))


def _flatten(suitable_for_isinstance):
    """
    isinstance() can accept a bunch of really annoying different types:
        * a single type
        * a tuple of types
        * an arbitrary nested tree of tuples

    Return a flattened tuple of the given argument.

    """

    types = set()

    if not isinstance(suitable_for_isinstance, tuple):
        suitable_for_isinstance = (suitable_for_isinstance,)
    for thing in suitable_for_isinstance:
        if isinstance(thing, tuple):
            types.update(_flatten(thing))
        else:
            types.add(thing)
    return tuple(types)


def _list(thing):
    """
    Wrap ``thing`` in a list if it's a single str.

    Otherwise, return it unchanged.

    """

    if isinstance(thing, basestring):
        return [thing]
    return thing


def _delist(thing):
    """
    Unwrap ``thing`` to a single element if its a single str in a list.

    Otherwise, return it unchanged.

    """

    if (
        isinstance(thing, list) and
        len(thing) == 1
        and isinstance(thing[0], basestring)
    ):
        return thing[0]
    return thing


def _unbool(element, true=object(), false=object()):
    """
    A hack to make True and 1 and False and 0 unique for _uniq.

    """

    if element is True:
        return true
    elif element is False:
        return false
    return element


def _uniq(container):
    """
    Check if all of a container's elements are unique.

    Successively tries first to rely that the elements are hashable, then
    falls back on them being sortable, and finally falls back on brute
    force.

    """

    try:
        return len(set(_unbool(i) for i in container)) == len(container)
    except TypeError:
        try:
            sort = sorted(_unbool(i) for i in container)
            sliced = itertools.islice(sort, 1, None)
            for i, j in zip(sort, sliced):
                if i == j:
                    return False
        except (NotImplementedError, TypeError):
            seen = []
            for e in container:
                e = _unbool(e)
                if e in seen:
                    return False
                seen.append(e)
    return True


def validate(instance, schema, cls=None, *args, **kwargs):
    if cls is None:
        cls = meta_schemas.get(schema.get("$schema"), Draft4Validator)
    cls.check_schema(schema)
    cls(schema, *args, **kwargs).validate(instance)
