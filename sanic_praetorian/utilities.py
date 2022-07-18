import functools
import inspect
import re
from typing import NoReturn
import warnings
import json

import segno

from sanic import Sanic
import pendulum

from sanic_praetorian.constants import RESERVED_CLAIMS
from sanic_praetorian.exceptions import (PraetorianError, ConfigurationError)


def is_valid_json(data: str) -> json:
    """
    Simple helper to validate if a value is valid json data
    """
    try:
        return json.loads(data)
    except ValueError:
        return False


def duration_from_string(text: str) -> pendulum:
    """
    Parses a duration from a string. String may look like these patterns:
    * 1 Hour
    * 7 days, 45 minutes
    * 1y11d20m

    An exception will be raised if the text cannot be parsed
    """
    text = text.replace(' ', '')
    text = text.replace(',', '')
    text = text.lower()
    match = re.match(
        r'''
            ((?P<years>\d+)y[a-z]*)?
            ((?P<months>\d+)mo[a-z]*)?
            ((?P<days>\d+)d[a-z]*)?
            ((?P<hours>\d+)h[a-z]*)?
            ((?P<minutes>\d+)m[a-z]*)?
            ((?P<seconds>\d+)s[a-z]*)?
        ''',
        text,
        re.VERBOSE,
    )
    ConfigurationError.require_condition(
        match,
        "Couldn't parse {}".format(text),
    )
    parts = match.groupdict()
    clean = {k: int(v) for (k, v) in parts.items() if v}
    ConfigurationError.require_condition(
        clean,
        "Couldn't parse {}".format(text),
    )
    with ConfigurationError.handle_errors("Couldn't parse {}".format(text)):
        return pendulum.duration(**clean)


def current_guard():
    """
    Fetches the current instance of :py:class:`Praetorian`
    that is attached to the current sanic app
    """
    guard = Sanic.get_app().ctx.extensions.get('praetorian', None)
    PraetorianError.require_condition(
        guard is not None,
        "No current guard found; Praetorian must be initialized first",
    )
    return guard


def app_context_has_jwt_data() -> bool:
    """
    Checks if there is already jwt_data added to the app context
    """
    return hasattr(Sanic.get_app().ctx, 'jwt_data')


def add_jwt_data_to_app_context(jwt_data) -> NoReturn:
    """
    Adds a dictionary of jwt data (presumably unpacked from a token) to the
    top of the sanic app's context
    """
    ctx = Sanic.get_app().ctx
    ctx.jwt_data = jwt_data


def get_jwt_data_from_app_context() -> str:
    """
    Fetches a dict of jwt token data from the top of the sanic app's context
    """
    ctx = Sanic.get_app().ctx
    jwt_data = getattr(ctx, 'jwt_data', None)
    PraetorianError.require_condition(
        jwt_data is not None,
        """
        No jwt_data found in app context.
        Make sure @auth_required decorator is specified *first* for route
        """,
    )
    return jwt_data


def remove_jwt_data_from_app_context() -> NoReturn:
    """
    Removes the dict of jwt token data from the top of the sanic app's context
    """
    ctx = Sanic.get_app().ctx
    if app_context_has_jwt_data():
        del ctx.jwt_data


def current_user_id() -> str:
    """
    This method returns the user id retrieved from jwt token data attached to
    the current sanic app's context
    """
    jwt_data = get_jwt_data_from_app_context()
    user_id = jwt_data.get('id', None)
    PraetorianError.require_condition(
        user_id is not None,
        "Could not fetch an id for the current user",
    )
    return user_id

def generate_totp_qr(user_totp: json) -> segno:
    """
    This is a helper utility to generate a :py:module:`segno`
    QR code renderer, based upon a supplied `User` TOTP value.
    """
    return segno.make(user_totp)

async def current_user() -> object:
    """
    This method returns a user instance for jwt token data attached to the
    current sanic app's context
    """
    user_id = current_user_id()
    guard = current_guard()
    user = await guard.user_class.identify(user_id)
    PraetorianError.require_condition(
        user is not None,
        "Could not identify the current user from the current id",
    )
    return user


def current_rolenames() -> set:
    """
    This method returns the names of all roles associated with the current user
    """
    jwt_data = get_jwt_data_from_app_context()
    if 'rls' not in jwt_data:
        # This is necessary so our set arithmetic works correctly
        return set(['non-empty-but-definitely-not-matching-subset'])
    else:
        return set(r.strip() for r in jwt_data['rls'].split(','))


def current_custom_claims() -> dict:
    """
    This method returns any custom claims in the current jwt
    """
    jwt_data = get_jwt_data_from_app_context()
    return {k: v for (k, v) in jwt_data.items() if k not in RESERVED_CLAIMS}


def deprecated(reason):
    """
    This is a decorator which can be used to mark functions
    as deprecated. It will result in a warning being emitted
    when the function is used.

    If no param is passed, a generic message is returned

    :param: reason: The reason for the raised Warning message

    Copied from https://stackoverflow.com/questions/40301488
    """

    if isinstance(reason, str):

        # The @deprecated is used with a 'reason'.
        #
        # .. code-block:: python
        #
        #    @deprecated("please, use another function")
        #    def old_function(x, y):
        #      pass

        def decorator(func1):

            if inspect.isclass(func1):
                fmt1 = "Call to deprecated class {name} ({reason})."
            else:
                fmt1 = "Call to deprecated function {name} ({reason})."

            @functools.wraps(func1)
            def new_func1(*args, **kwargs):
                warnings.simplefilter('always', DeprecationWarning)
                warnings.warn(
                    fmt1.format(name=func1.__name__, reason=reason),
                    category=DeprecationWarning,
                    stacklevel=2
                )
                warnings.simplefilter('default', DeprecationWarning)
                return func1(*args, **kwargs)

            return new_func1

        return decorator

    elif inspect.isclass(reason) or inspect.isfunction(reason):

        # The @deprecated is used without any 'reason'.
        #
        # .. code-block:: python
        #
        #    @deprecated
        #    def old_function(x, y):
        #      pass

        func2 = reason

        if inspect.isclass(func2):
            fmt2 = "Call to deprecated class {name}."
        else:
            fmt2 = "Call to deprecated function {name}."

        @functools.wraps(func2)
        def new_func2(*args, **kwargs):
            warnings.simplefilter('always', DeprecationWarning)
            warnings.warn(
                fmt2.format(name=func2.__name__),
                category=DeprecationWarning,
                stacklevel=2
            )
            warnings.simplefilter('default', DeprecationWarning)
            return func2(*args, **kwargs)

        return new_func2

    else:
        raise TypeError(repr(type(reason)))
