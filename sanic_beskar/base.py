from importlib import import_module
from importlib.util import find_spec
import datetime
import io
from turtle import pen
import jinja2
import jwt
import pendulum
import re
import textwrap
import uuid
import ujson

from collections.abc import Callable
from typing import Union, Optional

from sanic import Sanic
from sanic.log import logger

from passlib.context import CryptContext
from passlib.totp import TOTP

from sanic_beskar.utilities import (
    duration_from_string,
    is_valid_json,
    get_request,
)

from sanic_beskar.exceptions import (
    AuthenticationError,
    BlacklistedError,
    ClaimCollisionError,
    EarlyRefreshError,
    ExpiredAccessError,
    ExpiredRefreshError,
    InvalidRegistrationToken,
    InvalidResetToken,
    InvalidTokenHeader,
    InvalidUserError,
    LegacyScheme,
    MissingClaimError,
    MissingToken,
    MissingUserError,
    MisusedRegistrationToken,
    MisusedResetToken,
    ConfigurationError,
    BeskarError,
    TOTPRequired,
)

from sanic_beskar.constants import (
    DEFAULT_TOKEN_ACCESS_LIFESPAN,
    DEFAULT_JWT_ALGORITHM,
    DEFAULT_JWT_ALLOWED_ALGORITHMS,
    DEFAULT_TOKEN_PLACES,
    DEFAULT_TOKEN_COOKIE_NAME,
    DEFAULT_TOKEN_HEADER_NAME,
    DEFAULT_TOKEN_HEADER_TYPE,
    DEFAULT_TOKEN_REFRESH_LIFESPAN,
    DEFAULT_TOKEN_RESET_LIFESPAN,
    DEFAULT_USER_CLASS_VALIDATION_METHOD,
    DEFAULT_CONFIRMATION_TEMPLATE,
    DEFAULT_CONFIRMATION_SUBJECT,
    DEFAULT_RESET_TEMPLATE,
    DEFAULT_RESET_SUBJECT,
    DEFAULT_HASH_SCHEME,
    DEFAULT_HASH_ALLOWED_SCHEMES,
    DEFAULT_HASH_AUTOUPDATE,
    DEFAULT_HASH_AUTOTEST,
    DEFAULT_HASH_DEPRECATED_SCHEMES,
    DEFAULT_ROLES_DISABLED,
    IS_REGISTRATION_TOKEN_CLAIM,
    IS_RESET_TOKEN_CLAIM,
    REFRESH_EXPIRATION_CLAIM,
    RESERVED_CLAIMS,
    VITAM_AETERNUM,
    DEFAULT_TOTP_ENFORCE,
    DEFAULT_TOTP_SECRETS_TYPE,
    DEFAULT_TOTP_SECRETS_DATA,
    DEFAULT_TOKEN_PROVIDER,
    DEFAULT_PASETO_VERSION,
    AccessType,
)


class Beskar():
    """
    Comprises the implementation for the :py:mod:`sanic-beskar`
    :py:mod:`sanic` extension.  Provides a tool that allows password
    authentication and token provision for applications and designated
    endpoints
    """

    def __init__(
        self,
        app: Sanic = None,
        user_class: object = None,
        is_blacklisted: Callable = None,
        encode_token_hook: Callable = None,
        refresh_token_hook: Callable = None,
        rbac_populate_hook: Callable = None,
    ):
        self.app: Sanic = None
        self.pwd_ctx = None
        self.totp_ctx = None
        self.hash_scheme = None
        self.salt = None
        self.token_provider = 'jwt'
        self.paseto_ctx = None
        self.paseto_key = None
        self.paseto_token = None
        self.rbac_definitions = dict()

        if app is not None and user_class is not None:
            self.init_app(
                app,
                user_class,
                is_blacklisted,
                encode_token_hook,
                refresh_token_hook,
                rbac_populate_hook,
            )

    async def open_session(self, request):
        pass

    def init_app(
        self,
        app: Sanic = None,
        user_class: object = None,
        is_blacklisted: Callable = None,
        encode_token_hook: Callable = None,
        refresh_token_hook: Callable = None,
        rbac_populate_hook: Callable = None,
    ):
        """
        Initializes the :py:class:`Beskar` extension

        Args:
            app (Sanic): The :py:mod:`Sanic` app to bind this extention. Defaults to None.
            user_class (object): Class used to interact with a `User`. Defaults to None.
            is_blacklisted (Callable, optional): A method that may optionally be
                used to check the token against a blacklist when access or refresh
                is requested should take the jti for the token to check as a single
                argument. Returns True if the jti is blacklisted, False otherwise.
                Defaults to `False`.
            encode_token_hook (Callable, optional): A method that may optionally be
                called right before an encoded jwt is generated. Should take
                payload_parts which contains the ingredients for the jwt.
                Defaults to `None`.
            refresh_token_hook (Callable, optional): A method that may optionally be called
                right before an encoded jwt is refreshed. Should take payload_parts
                which contains the ingredients for the jwt. Defaults to `None`.
            rbac_populate_hook (Callable, optional): A method that may optionally be called
                at Beskar init time, or periodcally, to populate a RBAC dictionary mapping
                user Roles to RBAC rights. Defaults to `None`.

        Raises:
            ConfigurationError: Invalid/missing configuration value is detected.

        Returns:
            Object: Initialized sanic-beskar object.
        """

        self.app = app
        app.register_middleware(self.open_session, 'request')

        ConfigurationError.require_condition(
            app.config.get("SECRET_KEY") is not None,
            "There must be a SECRET_KEY app config setting set",
        )

        self.roles_disabled = app.config.get(
            "BESKAR_ROLES_DISABLED",
            DEFAULT_ROLES_DISABLED,
        )

        self.hash_autoupdate = app.config.get(
            "BESKAR_HASH_AUTOUPDATE",
            DEFAULT_HASH_AUTOUPDATE,
        )

        self.hash_autotest = app.config.get(
            "BESKAR_HASH_AUTOTEST",
            DEFAULT_HASH_AUTOTEST,
        )

        self.pwd_ctx = CryptContext(
            schemes=app.config.get(
                "BESKAR_HASH_ALLOWED_SCHEMES",
                DEFAULT_HASH_ALLOWED_SCHEMES,
            ),
            default=app.config.get(
                "BESKAR_HASH_SCHEME",
                DEFAULT_HASH_SCHEME,
            ),
            deprecated=app.config.get(
                "BESKAR_HASH_DEPRECATED_SCHEMES",
                DEFAULT_HASH_DEPRECATED_SCHEMES,
            ),
        )

        valid_schemes = self.pwd_ctx.schemes()
        ConfigurationError.require_condition(
            self.hash_scheme in valid_schemes or self.hash_scheme is None,
            f'If {"BESKAR_HASH_SCHEME"} is set, it must be one of the following schemes: {valid_schemes}'
        )

        if self.pwd_ctx.default_scheme().startswith('pbkdf2_'):
            if not find_spec('fastpbkdf2'):
                logger.warning(
                    textwrap.dedent(
                        """
                        You are using a `pbkdf2` hashing scheme, but didn't instll
                          the `fastpbkdf2` module, which will give you like 40%
                          speed improvements. you should go do that now.
                        """
                    )
                )

        self.user_class = self._validate_user_class(user_class)
        self.is_blacklisted = is_blacklisted or (lambda t: False)
        self.encode_token_hook = encode_token_hook
        self.refresh_token_hook = refresh_token_hook
        self.rbac_populate_hook = rbac_populate_hook

        self.encode_key = app.config["SECRET_KEY"]
        self.allowed_algorithms = app.config.get(
            "JWT_ALLOWED_ALGORITHMS",
            DEFAULT_JWT_ALLOWED_ALGORITHMS,
        )
        self.encode_algorithm = app.config.get(
            "JWT_ALGORITHM",
            DEFAULT_JWT_ALGORITHM,
        )
        self.access_lifespan = app.config.get(
            "TOKEN_ACCESS_LIFESPAN",
            DEFAULT_TOKEN_ACCESS_LIFESPAN,
        )
        self.refresh_lifespan = app.config.get(
            "TOKEN_REFRESH_LIFESPAN",
            DEFAULT_TOKEN_REFRESH_LIFESPAN,
        )
        self.reset_lifespan = app.config.get(
            "TOKEN_RESET_LIFESPAN",
            DEFAULT_TOKEN_RESET_LIFESPAN,
        )
        self.token_places = app.config.get(
            "TOKEN_PLACES",
            DEFAULT_TOKEN_PLACES,
        )
        self.cookie_name = app.config.get(
            "TOKEN_COOKIE_NAME",
            DEFAULT_TOKEN_COOKIE_NAME,
        )
        self.header_name = app.config.get(
            "TOKEN_HEADER_NAME",
            DEFAULT_TOKEN_HEADER_NAME,
        )
        self.header_type = app.config.get(
            "TOKEN_HEADER_TYPE",
            DEFAULT_TOKEN_HEADER_TYPE,
        )
        self.user_class_validation_method = app.config.get(
            "USER_CLASS_VALIDATION_METHOD",
            DEFAULT_USER_CLASS_VALIDATION_METHOD,
        )

        self.confirmation_template = app.config.get(
            "BESKAR_CONFIRMATION_TEMPLATE",
            DEFAULT_CONFIRMATION_TEMPLATE,
        )
        self.confirmation_uri = app.config.get(
            "BESKAR_CONFIRMATION_URI",
        )
        self.confirmation_sender = app.config.get(
            "BESKAR_CONFIRMATION_SENDER",
        )
        self.confirmation_subject = app.config.get(
            "BESKAR_CONFIRMATION_SUBJECT",
            DEFAULT_CONFIRMATION_SUBJECT,
        )

        self.reset_template = app.config.get(
            "BESKAR_RESET_TEMPLATE",
            DEFAULT_RESET_TEMPLATE,
        )
        self.reset_uri = app.config.get(
            "BESKAR_RESET_URI",
        )
        self.reset_sender = app.config.get(
            "BESKAR_RESET_SENDER",
        )
        self.reset_subject = app.config.get(
            "BESKAR_RESET_SUBJECT",
            DEFAULT_RESET_SUBJECT,
        )
        self.totp_enforce = app.config.get(
            "BESKAR_TOTP_ENFORCE",
            DEFAULT_TOTP_ENFORCE,
        )
        self.totp_secrets_type = app.config.get(
            "BESKAR_TOTP_SECRETS_TYPE",
            DEFAULT_TOTP_SECRETS_TYPE,
        )
        self.totp_secrets_data = app.config.get(
            "BESKAR_TOTP_SECRETS_DATA",
            DEFAULT_TOTP_SECRETS_DATA,
        )
        self.token_provider = app.config.get(
            "BESKAR_TOKEN_PROVIDER",
            DEFAULT_TOKEN_PROVIDER,
        )
        self.token_provider = self.token_provider.lower()
        self.paseto_version = app.config.get(
            "BESKAR_PASETO_VERSION",
            DEFAULT_PASETO_VERSION,
        )
        self.paseto_key = app.config.get(
            "BESKAR_PASETO_KEY",
            self.encode_key,
        )

        if isinstance(self.access_lifespan, dict):
            self.access_lifespan = pendulum.duration(**self.access_lifespan)
        elif isinstance(self.access_lifespan, str):
            self.access_lifespan = duration_from_string(self.access_lifespan)
        ConfigurationError.require_condition(
            isinstance(self.access_lifespan, datetime.timedelta),
            "access lifespan was not configured",
        )

        if isinstance(self.refresh_lifespan, dict):
            self.refresh_lifespan = pendulum.duration(**self.refresh_lifespan)
        if isinstance(self.refresh_lifespan, str):
            self.refresh_lifespan = duration_from_string(self.refresh_lifespan)
        ConfigurationError.require_condition(
            isinstance(self.refresh_lifespan, datetime.timedelta),
            "refresh lifespan was not configured",
        )

        ConfigurationError.require_condition(
            getattr(self, f"encode_{self.token_provider}_token"),
            "Invalid `token_provider` configured. Please check docs and try again.",
        )
        ConfigurationError.require_condition(
            self.paseto_version > 0 < 4,
            "Invalid `paseto_version` configured. Valid are [1, 2, 3, 4] only.",
        )

        if self.token_provider == 'paseto':
            from pyseto import Key, Paseto, Token # noqa

            self.paseto_key = Key.new(version=self.paseto_version, purpose="local", key=self.paseto_key)
            self.paseto_ctx = Paseto(exp=self.access_lifespan.seconds, include_iat=False)
            self.paseto_token = Token

        # TODO: add 'issuser', at the very least
        if self.totp_secrets_type:
            """
            If we are saying we are using a TOTP secret protection type,
            we need to ensure the type is something supported (file, string, wallet),
            and that the BESKAR_TOTP_SECRETS_DATA is populated.
            """
            self.totp_secrets_type = self.totp_secrets_type.lower()

            ConfigurationError.require_condition(
                self.totp_secrets_data,
                'If "BESKAR_TOTP_SECRETS_TYPE" is set, you must also'
                'provide a valid value for "BESKAR_TOTP_SECRETS_DATA"'
            )
            if self.totp_secrets_type == 'file':
                self.totp_ctx = TOTP.using(secrets_path=app.config.get("BESKAR_TOTP_SECRETS_DATA"))
            elif self.totp_secrets_type == 'string':
                self.totp_ctx = TOTP.using(secrets=app.config.get("BESKAR_TOTP_SECRETS_DATA"))
            elif self.totp_secrets_type == 'wallet':
                self.totp_ctx = TOTP.using(wallet=app.config.get("BESKAR_TOTP_SECRETS_DATA"))
            else:
                raise ConfigurationError(
                    f'If {"BESKAR_TOTP_SECRETS_TYPE"} is set, it must be one'
                    f'of the following schemes: {["file", "string", "wallet"]}'
                )
        else:
            self.totp_ctx = TOTP.using()

        self.is_testing = app.config.get("TESTING", False)

        if self.rbac_populate_hook:
            ConfigurationError.require_condition(
                callable(self.rbac_populate_hook),
                "rbac_populate_hook was configured, but doesn't appear callable",
            )

            @app.signal("beskar.rbac.update")
            async def rbac_populate():
                self.rbac_definitions = await self.rbac_populate_hook()
                logger.debug(f"RBAC definitions updated: {self.rbac_definitions}")

            @app.before_server_start
            async def init_rbac_populate(app):
                logger.info("Populating initial RBAC definitions")
                await app.dispatch("beskar.rbac.update")
            app.add_task(init_rbac_populate(app))

        if not hasattr(app.ctx, "extensions"):
            app.ctx.extensions = dict()
        app.ctx.extensions["beskar"] = self

        return app

    def _validate_user_class(self, user_class):
        """
        Validates the supplied :py:data:`user_class` to make sure that it has the
        class methods and attributes necessary to function correctly.
        After validating class methods, will attempt to instantiate a dummy
        instance of the user class to test for the requisite attributes

        Requirements:
        - :py:meth:`lookup` method. Accepts a string parameter, returns instance
        - :py:meth:`identify` method. Accepts an identity parameter, returns instance
        - :py:attribue:`identity` attribute. Provides unique id for the instance
        - :py:attribute:`rolenames` attribute. Provides list of roles attached to instance
        - :py:attribute:`password` attribute. Provides hashed password for instance

        Args:
            user_class (:py:class:`User`): `User` class to use.

        Returns:
            User: Validated `User` object

        Raises:
            :py:exc:`~sanic_beskar.exceptions.BeskarError`: Missing requirements
        """

        BeskarError.require_condition(
            getattr(user_class, "lookup", None) is not None,
            textwrap.dedent(
                """
                The user_class must have a lookup class method:
                user_class.lookup(<str>) -> <user instance>
                """
            ),
        )
        BeskarError.require_condition(
            getattr(user_class, "identify", None) is not None,
            textwrap.dedent(
                """
                The user_class must have an identify class method:
                user_class.identify(<identity>) -> <user instance>
                """
            ),
        )

        dummy_user = None
        try:
            dummy_user = user_class()
        except Exception:
            logger.debug(
                "Skipping instance validation because "
                "user cannot be instantiated without arguments"
            )
        if dummy_user:
            BeskarError.require_condition(
                hasattr(dummy_user, "identity"),
                textwrap.dedent(
                    """
                    Instances of user_class must have an identity attribute:
                    user_instance.identity -> <unique id for instance>
                    """
                ),
            )
            BeskarError.require_condition(
                self.roles_disabled or hasattr(dummy_user, "rolenames"),
                textwrap.dedent(
                    """
                    Instances of user_class must have a rolenames attribute:
                    user_instance.rolenames -> [<role1>, <role2>, ...]
                    """
                ),
            )
            BeskarError.require_condition(
                hasattr(dummy_user, "password"),
                textwrap.dedent(
                    """
                    Instances of user_class must have a password attribute:
                    user_instance.rolenames -> <hashed password>
                    """
                ),
            )

        return user_class

    async def generate_user_totp(self) -> object:
        """
        Generates a :py:mod:`passlib` TOTP for a user. This must be manually saved/updated to the
        :py:class:`User` object.

        . ..note:: The application secret(s) should be stored in a secure location, and each
         secret should contain a large amount of entropy (to prevent brute-force attacks
         if the encrypted keys are leaked).  :py:func:`passlib.generate_secret()` is
         provided as a convenience helper to generate a new application secret of suitable size.
         Best practice is to load these values from a file via secrets_path, pulled in value, or
         utilizing a `passlib wallet`, and then have your application give up permission
         to read this file once it's running.

        :returns: New :py:mod:`passlib` TOTP secret object
        """
        if not self.app.config.get("BESKAR_TOTP_SECRETS_TYPE"):
            logger.warning(
                textwrap.dedent(
                    """
                    Sanic_Beskar is attempting to generate a new TOTP
                    for a user, but you haven't configured a BESKAR_TOTP_SECRETS_TYPE
                    value, which means you aren't properly encrypting these stored
                    TOTP secrets. *tsk*tsk*
                    """
                )
            )

        return self.totp_ctx.new()

    async def _verify_totp(self, token: str, user: object):
        """
        Verifies that a plaintext password matches the hashed version of that
        password using the stored :py:mod:`passlib` password context
        """
        BeskarError.require_condition(
            self.totp_ctx is not None,
            "Beskar must be initialized before this method is available",
        )
        totp_factory = self.totp_ctx.new()

        """
        Optionally, if a :py:class:`User` model has a :py:meth:`get_cache_verify` method,
        call it, and use that response as the :py:data:`last_counter` value.
        """
        _last_counter = None
        if hasattr(user, 'get_cache_verify') and callable(user.get_cache_verify):
            _last_counter = await user.get_cache_verify()
        verify = totp_factory.verify(token, user.totp,
                                     last_counter=_last_counter)

        """
        Optionally, if our User model has a :py:func:`cache_verify` function,
        call it, providing the good verification :py:data:`counter` and
        :py:data:`cache_seconds` to be stored by :py:func:`cache_verify` function.

        This is for security against replay attacks, and should ideally be kept
        in a cache, but can be stored in the db
        """
        if hasattr(verify, 'counter'):
            if hasattr(user, 'cache_verify') and callable(user.cache_verify):
                logger.debug('Updating `User` token verify cache')
                await user.cache_verify(counter=verify.counter, seconds=verify.cache_seconds)

        return verify

    async def authenticate_totp(self, username: Union[str, object], token: str):
        """
        Verifies that a TOTP validates agains the stored TOTP for that
        username.

        If verification passes, the matching user instance is returned.

        If automatically called by :py:func:`authenticate`,
        it accepts a :py:class:`User` object instead of :py:data:`username`
        and skips the :py:func:`lookup` call.

        Args:
            username (Union[str, object]): Username, or `User` object to
                perform TOTP authentication against.
            token (str): TOTP token value to validate.

        Returns:
            :py:class:`User`: Validated `User` object.

        Raises:
            AuthenticationError: Failed TOTP authentication attempt.
        """

        BeskarError.require_condition(
            self.user_class is not None,
            "Beskar must be initialized before this method is available",
        )

        """
        If we are called from `authenticate`, we already looked up the user,
            don't waste the DB call again.
        """
        if isinstance(username, str):
            user = await self.user_class.lookup(username=username)
        else:
            user = username

        AuthenticationError.require_condition(
            user is not None
            and hasattr(user, 'totp')
            and user.totp
            and await is_valid_json(user.totp),
            "TOTP challenge is not properly configured for this user",
        )
        AuthenticationError.require_condition(
            user is not None
            and token is not None
            and await self._verify_totp(
                token,
                user,
            ),
            "The credentials provided are missing or incorrect",
        )

        return user

    async def authenticate(self, username: str, password: str, token: str = None):
        """
        Verifies that a password matches the stored password for that username.
        If verification passes, the matching user instance is returned

        .. note:: If :py:data:`BESKAR_TOTP_ENFORCE` is set to `True`
                  (default), and a user has a TOTP configuration, this call
                  must include the `token` value, or it will raise a
                  :py:exc:`~sanic_beskar.exceptions.TOTPRequired` exception
                  and not return the user.

                  This means either you will need to call it again, providing
                  the `token` value from the user, or seperately call
                  :py:func:`authenticate_totp`,
                  which only performs validation of the `token` value,
                  and not the users password.

                  **Choose your own adventure.**

        Args:
            username (str): Username to authenticate
            password (str): Password to validate against
            token (str, optional): TOTP Token value to validate against.
                Defaults to None.

        Raises:
            AuthenticationError: Failed password, TOTP, or password+TOTP attempt.
            TOTPRequired: Account is required to supply TOTP.

        Returns:
            :py:class:`User`: Authenticated `User` object.
        """

        BeskarError.require_condition(
            self.user_class is not None,
            "Beskar must be initialized before this method is available",
        )
        user = await self.user_class.lookup(username=username)
        AuthenticationError.require_condition(
            user is not None
            and self._verify_password(
                password,
                user.password,
            ),
            "The credentials provided are missing or incorrect",
        )

        """
        If we provided a TOTP token in this `authenicate` call,
            or if the user is required to use TOTP, instead of
            as a seperate call to `authenticate_totp`, then lets do it here.
        Failure to provide a TOTP token, when the user is required to use
            TOTP, results in a `TOTPRequired` exception, and the calling
            application will be required to either re-call `authenticate`
            with all 3 arugements, or call `authenticate_otp` directly.
        """
        if hasattr(user, 'totp') or token:
            if token:
                user = await self.authenticate_totp(username, token)
            elif self.totp_enforce:
                raise TOTPRequired("Password authentication successful -- "
                                   f"TOTP still *required* for user '{user.username}'.")

        """
        If we are set to BESKAR_HASH_AUTOUPDATE then check our hash
            and if needed, update the user.  The developer is responsible
            for using the returned user object and updating the data
            storage endpoint.

        Else, if we are set to BESKAR_HASH_AUTOTEST then check out hash
            and return exception if our hash is using the wrong scheme,
            but don't modify the user.
        """
        if self.hash_autoupdate:
            await self.verify_and_update(user=user, password=password)
        elif self.hash_autotest:
            await self.verify_and_update(user=user)

        return user

    def _verify_password(self, raw_password: str, hashed_password: str):
        """
        Verifies that a plaintext password matches the hashed version of that
        password using the stored :py:mod:`passlib` password context
        """
        BeskarError.require_condition(
            self.pwd_ctx is not None,
            "Beskar must be initialized before this method is available",
        )
        return self.pwd_ctx.verify(raw_password, hashed_password)

    def _check_user(self, user: object):
        """
        Checks to make sure that a user is valid. First, checks that the user
        is not None. If this check fails, a MissingUserError is raised. Next,
        checks if the user has a validation method. If the method does not
        exist, the check passes. If the method exists, it is called. If the
        result of the call is not truthy, a
        :py:exc:`~sanic_beskar.exceptions.InvalidUserError` is raised.
        """
        MissingUserError.require_condition(
            user is not None,
            "Could not find the requested user",
        )
        user_validate_method = getattr(
            user, self.user_class_validation_method, None
        )
        if user_validate_method is None:
            return
        InvalidUserError.require_condition(
            user_validate_method(),
            "The user is not valid or has had access revoked",
        )

    async def encode_paseto_token(
        self,
        user,
        override_access_lifespan: Optional[pendulum.Duration] = None,
        override_refresh_lifespan: Optional[pendulum.Duration] = None,
        bypass_user_check: Optional[bool] = False,
        is_registration_token: Optional[bool] =False,
        is_reset_token: Optional[bool] =False,
        **custom_claims
    ):
        """
        Encodes user data into a PASETO token that can be used for authorization
        at protected endpoints

        .. note:: Note that any claims supplied as `custom_claims` here must be
          :py:mod:`json` compatible types.

        Args:
            user (:py:class:`User`): `User` to generate a token for.
            override_access_lifespan (pendulum.Duration, optional): Override's the
                instance's access lifespan to set a custom duration after which
                the new token's accessability will expire. May not exceed the
                :py:data:`refresh_lifespan`. Defaults to `None`.
            override_refresh_lifespan (pendulum.Duration, optional): Override's the
                instance's refresh lifespan to set a custom duration after which
                the new token's refreshability will expire. Defaults to `None`.
            bypass_user_check (bool, optional): Override checking the user for
                being real/active.  Used for registration token generation.
                Defaults to `False`.
            is_registration_token (bool, optional): Indicates that the token will
                be used only for email-based registration. Defaults to `False`.
            is_reset_token (bool, optional): Indicates that the token will
                be used only for lost password reset. Defaults to `False`.
            custom_claims (dict, optional): Additional claims that should be packed
                in the payload. Defaults to `None`.

        Returns:
            str: Encoded PASETO token string.

        Raises:
            ClaimCollisionError: Tried to supply a RESERVED_CLAIM in the `custom_claims`.
        """

        ClaimCollisionError.require_condition(
            set(custom_claims.keys()).isdisjoint(RESERVED_CLAIMS),
            "The custom claims collide with required claims",
        )
        if not bypass_user_check:
            self._check_user(user)

        moment = pendulum.now("UTC")
        if override_refresh_lifespan is None:
            refresh_lifespan = self.refresh_lifespan
        else:
            refresh_lifespan = override_refresh_lifespan
        refresh_expiration = (moment + refresh_lifespan).int_timestamp

        if override_access_lifespan is None:
            access_lifespan = self.access_lifespan
        else:
            access_lifespan = override_access_lifespan
        access_expiration = min(
            (moment + access_lifespan).int_timestamp,
            refresh_expiration,
        )

        payload_parts = {
            "iat": moment.int_timestamp,
            "exp": access_expiration,
            "jti": str(uuid.uuid4()),
            "id": user.identity,
            "rls": ",".join(user.rolenames),
            REFRESH_EXPIRATION_CLAIM: refresh_expiration,
        }
        if is_registration_token:
            payload_parts[IS_REGISTRATION_TOKEN_CLAIM] = True
        if is_reset_token:
            payload_parts[IS_RESET_TOKEN_CLAIM] = True
        logger.debug(
            f"Attaching custom claims: {custom_claims}",
        )
        payload_parts.update(custom_claims)

        if self.encode_token_hook:
            self.encode_token_hook(**payload_parts)

        # PASETO stores its own EXP as seconds from now()
        time_delta = access_expiration - moment.int_timestamp

        return self.paseto_ctx.encode(
            self.paseto_key,
            payload_parts,
            serializer=ujson,
            exp=time_delta,
        ).decode('utf-8')  # bytes by default, which are ugly

    async def encode_jwt_token(
        self,
        user,
        override_access_lifespan: Optional[pendulum.duration] = None,
        override_refresh_lifespan: Optional[pendulum.duration] = None,
        bypass_user_check: Optional[bool] = False,
        is_registration_token: Optional[bool] = False,
        is_reset_token: Optional[bool] = False,
        **custom_claims
    ):
        """
        Encodes user data into a jwt token that can be used for authorization
        at protected endpoints

        Args:
            user (:py:class:`User`): `User` to generate a token for.
            override_access_lifespan (pendulum.Duration, optional): Override's the
                instance's access lifespan to set a custom duration after which
                the new token's accessability will expire. May not exceed the
                :py:data:`refresh_lifespan`. Defaults to `None`.
            override_refresh_lifespan (pendulum.Duration, optional): Override's the
                instance's refresh lifespan to set a custom duration after which
                the new token's refreshability will expire. Defaults to `None`.
            bypass_user_check (bool, optional): Override checking the user for
                being real/active.  Used for registration token generation.
                Defaults to `False`.
            is_registration_token (bool, optional): Indicates that the token will
                be used only for email-based registration. Defaults to `False`.
            is_reset_token (bool, optional): Indicates that the token will
                be used only for lost password reset. Defaults to `False`.
            custom_claims (dict, optional): Additional claims that should be packed
                in the payload. Defaults to `None`.

        Returns:
            str: Encoded JWT token string.

        Raises:
            ClaimCollisionError: Tried to supply a RESERVED_CLAIM in the `custom_claims`.
        """

        ClaimCollisionError.require_condition(
            set(custom_claims.keys()).isdisjoint(RESERVED_CLAIMS),
            "The custom claims collide with required claims",
        )
        if not bypass_user_check:
            self._check_user(user)

        moment = pendulum.now("UTC")

        if override_refresh_lifespan is None:
            refresh_lifespan = self.refresh_lifespan
        else:
            refresh_lifespan = override_refresh_lifespan
        refresh_expiration = (moment + refresh_lifespan).int_timestamp

        if override_access_lifespan is None:
            access_lifespan = self.access_lifespan
        else:
            access_lifespan = override_access_lifespan
        access_expiration = min(
            (moment + access_lifespan).int_timestamp,
            refresh_expiration,
        )

        payload_parts = {
            "iat": moment.int_timestamp,
            "exp": access_expiration,
            "jti": str(uuid.uuid4()),
            "id": user.identity,
            "rls": ",".join(user.rolenames),
            REFRESH_EXPIRATION_CLAIM: refresh_expiration,
        }
        if is_registration_token:
            payload_parts[IS_REGISTRATION_TOKEN_CLAIM] = True
        if is_reset_token:
            payload_parts[IS_RESET_TOKEN_CLAIM] = True
        logger.debug(
            f"Attaching custom claims: {custom_claims}"
        )
        payload_parts.update(custom_claims)

        if self.encode_token_hook:
            self.encode_token_hook(**payload_parts)
        return jwt.encode(
            payload_parts,
            self.encode_key,
            self.encode_algorithm,
        )

    async def encode_token(
        self,
        user: object,
        override_access_lifespan: Optional[pendulum.Duration] = None,
        override_refresh_lifespan: Optional[pendulum.Duration] = None,
        bypass_user_check: Optional[bool] = False,
        is_registration_token: Optional[bool] = False,
        is_reset_token: Optional[bool] = False,
        **custom_claims
    ):
        """
        Wrapper function to encode user data into a `insert_type_here` token
        that can be used for authorization at protected endpoints.

        Calling this will allow your app configuration to automagically create
        the appropriate token type.

        Args:
            user (:py:class:`User`): `User` to generate a token for.
            override_access_lifespan (pendulum.Duration, optional): Override's the
                instance's access lifespan to set a custom duration after which
                the new token's accessability will expire. May not exceed the
                :py:data:`refresh_lifespan`. Defaults to `None`.
            override_refresh_lifespan (pendulum.Duration, optional): Override's the
                instance's refresh lifespan to set a custom duration after which
                the new token's refreshability will expire. Defaults to `None`.
            bypass_user_check (bool, optional): Override checking the user for
                being real/active.  Used for registration token generation.
                Defaults to `False`.
            is_registration_token (bool, optional): Indicates that the token will
                be used only for email-based registration. Defaults to `False`.
            is_reset_token (bool, optional): Indicates that the token will
                be used only for lost password reset. Defaults to `False`.
            custom_claims (dict, optional): Additional claims that should be packed
                in the payload. Defaults to `None`.

        Returns:
            str: Encoded token string of application configuration type `TOKEN_PROVIDER`.

        Raises:
            ClaimCollisionError: Tried to supply a RESERVED_CLAIM in the `custom_claims`.
        """

        return await getattr(
            self,
            f"encode_{self.token_provider}_token"
        )(
            user,
            override_access_lifespan=override_access_lifespan,
            override_refresh_lifespan=override_refresh_lifespan,
            bypass_user_check=bypass_user_check,
            is_registration_token=is_registration_token,
            is_reset_token=is_reset_token,
            **custom_claims
        )

    async def encode_eternal_token(self, user, **custom_claims):
        """
        This utility function encodes an application configuration defined
        type token that never expires

        .. note:: This should be used sparingly since the token could become
                  a security concern if it is ever lost. If you use this
                  method, you should be sure that your application also
                  implements a blacklist so that a given token can be blocked
                  should it be lost or become a security concern

        Args:
            user (:py:class:`User`): `User` to generate a token for.
            custom_claims (dict, optional): Additional claims that should be packed
                in the payload. Defaults to `None`.

        Returns:
            str: Encoded, *never expiring*, token string of application configuration type `TOKEN_PROVIDER`.
        """

        return await self.encode_token(
            user,
            override_access_lifespan=VITAM_AETERNUM,
            override_refresh_lifespan=VITAM_AETERNUM,
            **custom_claims
        )

    async def refresh_token(self, token: str, override_access_lifespan=None):
        """
        Wrapper function to creates a new token for a user if and only if the old
        token's access permission is expired but its refresh permission is not yet
        expired. The new token's refresh expiration moment is the same as the old
        token's, but the new token's access expiration is refreshed.

        Token type is determined by application configuration, when using this
        helper function.

        Args:
            token (str): The existing token that needs to be replaced with a new,
                refreshed token.
            override_access_lifespan (_type_, optional): Override's the instance's
                access lifespan to set a custom duration after which the new
                token's accessability will expire. May not exceed the
                :py:data:`refresh_lifespan`. Defaults to `None`.

        Returns:
            str: Encoded token string of application configuration type `TOKEN_PROVIDER`.
        """

        return await getattr(
            self,
            f"refresh_{self.token_provider}_token"
        )(token=token, override_access_lifespan=override_access_lifespan)

    async def refresh_paseto_token(self, token: str, override_access_lifespan=None):
        """
        Creates a new PASETO token for a user if and only if the old token's access
        permission is expired but its refresh permission is not yet expired.
        The new token's refresh expiration moment is the same as the old
        token's, but the new token's access expiration is refreshed

        Args:
            token (str): The existing token that needs to be replaced with a new,
                refreshed token.
            override_access_lifespan (_type_, optional): Override's the instance's
                access lifespan to set a custom duration after which the new
                token's accessability will expire. May not exceed the
                :py:data:`refresh_lifespan`. Defaults to `None`.

        Returns:
            str: Encoded PASETO token string.
        """

        moment = pendulum.now("UTC")
        data = await self.extract_token(token, access_type=AccessType.refresh)

        user = await self.user_class.identify(data["id"])
        self._check_user(user)

        if override_access_lifespan is None:
            access_lifespan = self.access_lifespan
        else:
            access_lifespan = override_access_lifespan
        refresh_expiration = data[REFRESH_EXPIRATION_CLAIM]
        access_expiration = min(
            (moment + access_lifespan).int_timestamp,
            refresh_expiration,
        )

        custom_claims = {
            k: v for (k, v) in data.items() if k not in RESERVED_CLAIMS
        }
        payload_parts = {
            "iat": moment.int_timestamp,
            "exp": access_expiration,
            "jti": data["jti"],
            "id": data["id"],
            "rls": ",".join(user.rolenames),
            REFRESH_EXPIRATION_CLAIM: refresh_expiration,
        }
        payload_parts.update(custom_claims)

        if self.refresh_token_hook:
            self.refresh_token_hook(**payload_parts)

        # PASETO stores its own EXP as seconds from now()
        time_delta = access_expiration - moment.int_timestamp

        return self.paseto_ctx.encode(
            self.paseto_key,
            payload_parts,
            serializer=ujson,
            exp=time_delta,
        )

    async def refresh_jwt_token(self, token: str, override_access_lifespan=None):
        """
        Creates a new JWT token for a user if and only if the old token's access
        permission is expired but its refresh permission is not yet expired.
        The new token's refresh expiration moment is the same as the old
        token's, but the new token's access expiration is refreshed

        Args:
            token (str): The existing token that needs to be replaced with a new,
                refreshed token.
            override_access_lifespan (_type_, optional): Override's the instance's
                access lifespan to set a custom duration after which the new
                token's accessability will expire. May not exceed the
                :py:data:`refresh_lifespan`. Defaults to `None`.

        Returns:
            str: Encoded JWT token string.
        """

        moment = pendulum.now("UTC")
        data = await self.extract_token(token, access_type=AccessType.refresh)

        user = await self.user_class.identify(data["id"])
        self._check_user(user)

        if override_access_lifespan is None:
            access_lifespan = self.access_lifespan
        else:
            access_lifespan = override_access_lifespan
        refresh_expiration = data[REFRESH_EXPIRATION_CLAIM]
        access_expiration = min(
            (moment + access_lifespan).int_timestamp,
            refresh_expiration,
        )

        custom_claims = {
            k: v for (k, v) in data.items() if k not in RESERVED_CLAIMS
        }
        payload_parts = {
            "iat": moment.int_timestamp,
            "exp": access_expiration,
            "jti": data["jti"],
            "id": data["id"],
            "rls": ",".join(user.rolenames),
            REFRESH_EXPIRATION_CLAIM: refresh_expiration,
        }
        payload_parts.update(custom_claims)

        if self.refresh_token_hook:
            self.refresh_token_hook(**payload_parts)
        return jwt.encode(
            payload_parts,
            self.encode_key,
            self.encode_algorithm,
        )

    async def extract_token(self, token: str, access_type=AccessType.access):
        """
        Wrapper funciton to extract a data dictionary from a token. This
        function will automagically identify the token type based upon
        application configuration and process it accordingly.

        Args:
            token (str): Token to be processed
            access_type (AccessType): Type of token being processed

        Returns:
            dict: Extracted token as a `dict`
        """
        return await getattr(
            self,
            f"extract_{self.token_provider}_token"
        )(token=token, access_type=access_type)

    async def extract_paseto_token(self, token: object, access_type=AccessType.access):
        """
        Extracts a data dictionary from a PASETO token.

        Args:
            token (str): Token to be processed
            access_type (AccessType): Type of token being processed

        Returns:
            dict: Extracted token as a `dict`
        """

        # Note: we disable exp verification because we will do it ourselves
        failed = None
        keys = self.paseto_key if isinstance(self.paseto_key, list) else [self.paseto_key]
        t = self.paseto_token.new(token)
        for k in keys:
            if k.header != t.header:
                continue
            try:
                if k.purpose == "local":
                    t.payload = k.decrypt(t.payload, t.footer)
                else:
                    t.payload = k.verify(t.payload, t.footer)
                try:
                    t.payload = ujson.loads(t.payload)
                except Exception as err:
                    raise InvalidTokenHeader("Failed to deserialize the payload.") from err
            except Exception as err:
                failed = err
        if failed:
            raise failed

        # Convert to expected time format
        t.payload['exp'] = pendulum.parse(t.payload['exp']).int_timestamp
        self._validate_token_data(t.payload, access_type=access_type)
        return t.payload

    async def extract_jwt_token(self, token: str, access_type=AccessType.access):
        """
        Extracts a data dictionary from a JWT token.

        Args:
            token (str): Token to be processed
            access_type (AccessType): Type of token being processed

        Returns:
            dict: Extracted token as a `dict`
        """

        # Note: we disable exp verification because we will do it ourselves
        with InvalidTokenHeader.handle_errors("failed to decode JWT token"):
            data = jwt.decode(
                token,
                self.encode_key,
                algorithms=self.allowed_algorithms,
                options={"verify_exp": False},
            )
        self._validate_token_data(data, access_type=access_type)
        return data

    def _validate_token_data(self, data, access_type):
        """
        Validates that the data for a jwt token is valid
        """
        MissingClaimError.require_condition(
            "jti" in data,
            "Token is missing jti claim",
        )
        BlacklistedError.require_condition(
            not self.is_blacklisted(data["jti"]),
            "Token has a blacklisted jti",
        )
        MissingClaimError.require_condition(
            "id" in data,
            "Token is missing id field",
        )
        MissingClaimError.require_condition(
            "exp" in data,
            "Token is missing exp claim",
        )
        MissingClaimError.require_condition(
            REFRESH_EXPIRATION_CLAIM in data,
            f"Token is missing {REFRESH_EXPIRATION_CLAIM} claim",
        )
        moment = pendulum.now("UTC").int_timestamp
        if access_type == AccessType.access:
            MisusedRegistrationToken.require_condition(
                IS_REGISTRATION_TOKEN_CLAIM not in data,
                "registration token used for access",
            )
            MisusedResetToken.require_condition(
                IS_RESET_TOKEN_CLAIM not in data,
                "password reset token used for access",
            )
            ExpiredAccessError.require_condition(
                moment <= data["exp"],
                "access permission has expired",
            )
        elif access_type == AccessType.refresh:
            MisusedRegistrationToken.require_condition(
                IS_REGISTRATION_TOKEN_CLAIM not in data,
                "registration token used for refresh",
            )
            MisusedResetToken.require_condition(
                IS_RESET_TOKEN_CLAIM not in data,
                "password reset token used for refresh",
            )
            EarlyRefreshError.require_condition(
                moment > data["exp"],
                "access permission for token has not expired. may not refresh",
            )
            ExpiredRefreshError.require_condition(
                moment <= data[REFRESH_EXPIRATION_CLAIM],
                "refresh permission for token has expired",
            )
        elif access_type == AccessType.register:
            ExpiredAccessError.require_condition(
                moment <= data["exp"],
                "register permission has expired",
            )
            InvalidRegistrationToken.require_condition(
                IS_REGISTRATION_TOKEN_CLAIM in data,
                "invalid registration token used for verification",
            )
            MisusedResetToken.require_condition(
                IS_RESET_TOKEN_CLAIM not in data,
                "password reset token used for registration",
            )
        elif access_type == AccessType.reset:
            MisusedRegistrationToken.require_condition(
                IS_REGISTRATION_TOKEN_CLAIM not in data,
                "registration token used for reset",
            )
            ExpiredAccessError.require_condition(
                moment <= data["exp"],
                "reset permission has expired",
            )
            InvalidResetToken.require_condition(
                IS_RESET_TOKEN_CLAIM in data,
                "invalid reset token used for verification",
            )

    def _unpack_header(self, headers):
        """
        Unpacks a token from a request header
        """
        token_header = headers.get(self.header_name)
        MissingToken.require_condition(
            token_header is not None,
            f"Token not found in headers under '{self.header_name}'",
        )

        match = re.match(self.header_type + r"\s*([\w\.-]+)", token_header)
        InvalidTokenHeader.require_condition(
            match is not None,
            "Token header structure is invalid",
        )
        token = match.group(1)
        return token

    def read_token_from_header(self, request=None):
        """
        Unpacks a token from the current sanic request

        Args:
            request (Request): Current Sanic `Request`.

        Returns:
            str: Unpacked token from header.
        """

        _request = get_request(request)
        return self._unpack_header(_request.headers)

    def _unpack_cookie(self, cookies):
        """
        Unpacks a jwt token from a request cookies
        """

        token_cookie = cookies.get(self.cookie_name)
        MissingToken.require_condition(
            token_cookie is not None,
            f"Token not found in cookie under '{self.cookie_name}'"
        )
        return token_cookie

    def read_token_from_cookie(self, request=None):
        """
        Unpacks a token from the current sanic request

        Args:
            request (Request): Current Sanic `Request`.

        Returns:
            str: Unpacked token from cookie.
        """

        _request = get_request(request)
        return self._unpack_cookie(_request.cookies)

    def read_token(self, request=None):
        """
        Tries to unpack the token from the current sanic request
        in the locations configured by :py:data:`TOKEN_PLACES`.
        Check-Order is defined by the value order in :py:data:`TOKEN_PLACES`.

        Args:
            request (sanic.Request): Sanic ``request`` object

        Raises:
            :py:exc:`~sanic_beskar.exceptions.MissingToken` if token is not found in any
                :py:data:`~sanic_beskar.constants.TOKEN_PLACES`

        Returns:
            str: Token.
        """

        _request = get_request(request)
        for place in self.token_places:
            try:
                return getattr(
                    self,
                    f"read_token_from_{place.lower()}"
                )(_request)
            except MissingToken:
                pass
            except AttributeError:
                logger.warning(
                    textwrap.dedent(
                        f"""
                        Sanic_Beskar hasn't implemented reading tokens
                        from location {place.lower()}.
                        Please reconfigure TOKEN_PLACES.
                        Values accepted in TOKEN_PLACES are:
                        {DEFAULT_TOKEN_PLACES}
                        """
                    )
                )

        raise MissingToken(
            textwrap.dedent(
                f"""
                Could not find token in any
                 of the given locations: {self.token_places}
                """
            ).replace("\n", "")
        )

    async def pack_header_for_user(
        self,
        user,
        override_access_lifespan=None,
        override_refresh_lifespan=None,
        **custom_claims
    ):
        """
        Encodes a jwt token and packages it into a header dict for a given user

        Args:
            user (:py:class:`User`): The user to package the header for
            override_access_lifespan (:py:data:`pendulum`):  Override's the instance's
                access lifespan to set a custom duration after which the new token's
                accessability will expire. May not exceed the :py:data:`refresh_lifespan`
            override_refresh_lifespan (:py:data:`pendulum`): Override's the instance's
                refresh lifespan to set a custom duration after which the new token's
                refreshability will expire.
            custom_claims (dict): Additional claims that should be packed in the payload. Note
                that any claims supplied here must be :py:mod:`json` compatible types

        Returns:
            json: Updated header, including token
        """

        token = await self.encode_token(
            user,
            override_access_lifespan=override_access_lifespan,
            override_refresh_lifespan=override_refresh_lifespan,
            **custom_claims
        )
        return {self.header_name: f"{self.header_type} {token}"}

    async def send_registration_email(
        self,
        email: str,
        user: object,
        template: Optional[str] = None,
        confirmation_sender: Optional[str] = None,
        confirmation_uri: Optional[str] = None,
        subject: Optional[str] = None,
        override_access_lifespan: Optional[pendulum.duration] = None,
    ):
        """
        Sends a registration email to a new user, containing a time expiring
        token usable for validation.  This requires your application
        is initialized with a `mail` extension, which supports
        sanic-mailing's :py:class:`Message` object and a
        :py:meth:`send_message` method.

        Args:
            user (:py:class:`User`): The user object to tie claim to
                (username, id, email, etc)
            template (Optional, :py:data:`filehandle`): HTML Template for confirmation
                email. If not provided, a stock entry is used.
            confirmation_sender (Optional, str): The sender that shoudl be attached to the
                confirmation email. Overrides the :py:data:`BESKAR_CONFIRMATION_SENDER`
                config setting.
            confirmation_uri (Optional, str): The uri that should be visited to complete email
                registration. Should usually be a uri to a frontend or external service
                that calls a 'finalize' method in the api to complete registration. Will
                override the :py:data:`BESKAR_CONFIRMATION_URI` config setting.
            subject (Optional, str): The registration email subject.  Will override the
                :py:data:`BESKAR_CONFIRMATION_SUBJECT` config setting.
            override_access_lifespan (Optional, :py:data:`pendulum`): Overrides the
                :py:data:`TOKEN_ACCESS_LIFESPAN` to set an access lifespan for the
                registration token.

        Returns:
            dict: Summary of information sent, along with the `result` from mail send. (Essentually
            the response of :py:func:`send_token_email`).
        """

        if subject is None:
            subject = self.confirmation_subject

        if confirmation_uri is None:
            confirmation_uri = self.confirmation_uri

        sender = confirmation_sender or self.confirmation_sender

        logger.debug(
            f"Generating token with lifespan: {override_access_lifespan}"
        )
        custom_token = await self.encode_token(
            user,
            override_access_lifespan=override_access_lifespan,
            bypass_user_check=True,
            is_registration_token=True,
        )

        return await self.send_token_email(
            email,
            user=user,
            template=template,
            action_sender=sender,
            action_uri=confirmation_uri,
            subject=subject,
            custom_token=custom_token,
        )

    async def send_reset_email(
        self,
        email: str,
        template: Optional[str] = None,
        reset_sender: Optional[str] = None,
        reset_uri: Optional[str] = None,
        subject: Optional[str] = None,
        override_access_lifespan: Optional[pendulum.duration] = None,
    ):
        """
        Sends a password reset email to a user, containing a time expiring
        token usable for validation.  This requires your application
        is initialized with a :py:mod:`mail` extension, which supports
        sanic-mailing's :py:class:`Message` object and a
        :py:meth:`send_message()` method.

        Args:
            email (str): The email address to attempt to send to.
            template (Optional, :py:data:`filehandle`): HTML Template for reset email.
                If not provided, a stock entry is used.
            confirmation_sender (Optional, str): The sender that should be attached to the
                reset email. Defaults to :py:data:`BESKAR_RESET_SENDER` config setting.
            confirmation_uri (Optional, str): The uri that should be visited to complete password
                reset. Should usually be a uri to a frontend or external service that calls
                the 'validate_reset_token()' method in the api to complete reset. Defaults to
                :py:data:`BESKAR_RESET_URI` config setting.
            subject (Optional, str): The reset email subject. Defaults to
                :py:data:`BESKAR_RESET_SUBJECT` config setting.
            override_access_lifespan (Optional, :py:data:`pendulum`): Overrides the
                :py:data:`TOKEN_ACCESS_LIFESPAN` to set an access lifespan for the registration token.
                Defaults to :py:data:`TOKEN_ACCESS_LIFESPAN` config setting.

        Returns:
            dict: Summary of information sent, along with the `result` from mail send. (Essentually
            the response of :py:func:`send_token_email`).
        """
        if subject is None:
            subject = self.reset_subject

        if reset_uri is None:
            reset_uri = self.reset_uri

        sender = reset_sender or self.reset_sender

        user = await self.user_class.lookup(email=email)
        MissingUserError.require_condition(
            user is not None,
            "Could not find the requested user",
        )

        logger.debug(
            f"Generating token with lifespan: {override_access_lifespan}"
        )
        custom_token = await self.encode_token(
            user,
            override_access_lifespan=override_access_lifespan,
            bypass_user_check=False,
            is_reset_token=True,
        )

        return await self.send_token_email(
            user.email,
            user=user,
            template=template,
            action_sender=sender,
            action_uri=reset_uri,
            subject=subject,
            custom_token=custom_token,
        )

    async def send_token_email(
        self,
        email,
        user=None,
        template=None,
        action_sender=None,
        action_uri=None,
        subject=None,
        override_access_lifespan=None,
        custom_token=None,
    ):
        """
        Sends an email to a user, containing a time expiring
        token usable for several actions.  This requires
        your application is initialized with a `mail` extension,
        which supports sanic-mailing's :py:class:`Message` object and
        a :py:meth:`send_message` method.

        :returns: a :py:data:`dict` containing the information sent, along
                  with the ``result`` from mail send.
        :rtype: :py:data:`dict`

        :param email: The email address to use (username, id, email, etc)
        :type email: str
        :param user:  The user object to tie claim to (username, id, email, etc)
        :type user: :py:class:`User`
        :param template: HTML Template for confirmation email.
                          If not provided, a stock entry is used
        :type template: :py:data:`filehandle`
        :param action_sender: The sender that should be attached
                               to the confirmation email.
        :type action_sender: str
        :param action_uri: The uri that should be visited to complete the token action.
        :type action_uri: str
        :param subject: The email subject.
        :type subject: str
        :param override_access_lifespan: Overrides the :py:data:`TOKEN_ACCESS_LIFESPAN`
                                          to set an access lifespan for the
                                          registration token.
        :type override_access_lifespan: :py:data:`pendulum`
        :param custom_token: The token to be carried as the email's payload
        :type custom_token: str

        :raises: :py:exc:`~sanic_beskar.exceptions.BeskarError` if missing
                   required parameters
        """
        notification = {
            "result": None,
            "message": None,
            "user": str(user),
            "email": email,
            "token": custom_token,
            "subject": subject,
            "confirmation_uri": action_uri,  # backwards compatibility
            "action_uri": action_uri,
        }

        BeskarError.require_condition(
            self.app.ctx.mail,
            "Your app must have a mail extension enabled to register by email",
        )

        BeskarError.require_condition(
            action_sender,
            "A sender is required to send confirmation email",
        )

        BeskarError.require_condition(
            custom_token,
            "A custom_token is required to send notification email",
        )

        if template is None:
            with open(self.confirmation_template) as fh:
                template = fh.read()

        with BeskarError.handle_errors("fail sending email"):
            jinja_tmpl = jinja2.Template(template)
            notification["message"] = jinja_tmpl.render(notification).strip()

            Mail = import_module(self.app.ctx.mail.__module__)
            msg = Mail.Message(
                subject=notification["subject"],
                to=[notification["email"]],
                from_address=action_sender,
                html=notification["message"],
                reply_to=[action_sender],
            )

            logger.debug(f"Sending email to {email}")
            notification["result"] = await self.app.ctx.mail.send_message(
                msg
            )

        return notification

    async def get_user_from_registration_token(self, token: str):
        """
        Gets a user based on the registration token that is supplied. Verifies
        that the token is a regisration token and that the user can be properly
        retrieved

        :param token: Registration token to validate
        :type token: str

        :raises: :py:exc:`~sanic_beskar.exceptions.BeskarError` if missing
                   required parameters
        :returns: :py:class:`User` object of looked up user after token validation
        :rtype: :py:class:`User`
        """
        data = await self.extract_token(token, access_type=AccessType.register)
        user_id = data.get("id")
        BeskarError.require_condition(
            user_id is not None,
            "Could not fetch an id from the registration token",
        )
        user = await self.user_class.identify(user_id)
        BeskarError.require_condition(
            user is not None,
            "Could not identify the user from the registration token",
        )
        return user

    async def validate_reset_token(self, token: str):
        """
        Validates a password reset request based on the reset token
        that is supplied. Verifies that the token is a reset token
        and that the user can be properly retrieved

        :param token: Reset token to validate
        :type token: str

        :raises: :py:exc:`~sanic_beskar.exceptions.BeskarError` if missing
                   required parameters
        :returns: :py:class:`User` object of looked up user after token validation
        :rtype: :py:class:`User`
        """
        data = await self.extract_token(token, access_type=AccessType.reset)
        user_id = data.get("id")
        BeskarError.require_condition(
            user_id is not None,
            "Could not fetch an id from the reset token",
        )
        user = await self.user_class.identify(user_id)
        BeskarError.require_condition(
            user is not None,
            "Could not identify the user from the reset token",
        )
        return user

    def hash_password(self, raw_password: str):
        """
        Hashes a plaintext password using the stored passlib password context

        :param raw_password: cleartext password for the user
        :type raw_password: str

        :raises: :py:exc:`~sanic_beskareptions.BeskarError` if
                    no password is provided
        :returns: Properly hashed ciphertext of supplied :py:data:`raw_password`
        :rtype: str
        """
        BeskarError.require_condition(
            self.pwd_ctx is not None,
            "Beskar must be initialized before this method is available",
        )
        """
        `scheme` is now set with self.pwd_ctx.update(default=scheme) due
            to the depreciation in upcoming passlib 2.0.
         zillions of warnings suck.
        """
        return self.pwd_ctx.hash(raw_password)

    async def verify_and_update(self, user: object = None, password: str = None):
        """
        Validate a password hash contained in the user object is
        hashed with the defined hash scheme (:py:data:`BESKAR_HASH_SCHEME`).

        If not, raise an Exception of :py:exc:`~sanic_beskar.exceptions.LegacySchema`,
        unless the :py:data:`password` arguement is provided, in which case an updated
        :py:class:`User` will be returned, and must be saved by the calling app. The
        updated :py:class:`User` will contain the users current password updated to the
        currently desired hash scheme (:py:exc:`~BESKAR_HASH_SCHEME`).

        :param user:     The user object to tie claim to
                              (username, id, email, etc). *MUST*
                              include the password field,
                              defined as :py:attr:`password`
        :type user: object
        :param password: The user's provide password from login.
                              If present, this is used to validate
                              and then attempt to update with the
                              new :py:data:`BESKAR_HASH_SCHEME` scheme.
        :type password: str

        :returns: Authenticated :py:class:`User`
        :raises: :py:exc:`~sanic_beskar.exceptions.AuthenticationError` upon authentication failure
        """
        if self.pwd_ctx.needs_update(user.password):
            if password:
                (rv, updated) = self.pwd_ctx.verify_and_update(
                    password,
                    user.password,
                )
                AuthenticationError.require_condition(
                    rv,
                    "Could not verify password",
                )
                user.password = updated
            else:
                used_hash = self.pwd_ctx.identify(user.password)
                desired_hash = self.hash_scheme
                raise LegacyScheme(
                    f"Hash using non-current scheme '{used_hash}'."
                    f"Use '{desired_hash}' instead."
                )

        return user
