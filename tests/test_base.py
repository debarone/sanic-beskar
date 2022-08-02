import warnings
import pendulum
import plummet
import pytest
import ujson

from httpx import Cookies

from passlib.totp import generate_secret

from passlib.exc import (
    InvalidTokenError,
    MalformedTokenError,
    UsedTokenError,
)

from sanic.log import logger

from sanic_beskar import Beskar
from sanic_beskar.exceptions import (
    AuthenticationError,
    BlacklistedError,
    ClaimCollisionError,
    EarlyRefreshError,
    ExpiredAccessError,
    ExpiredRefreshError,
    InvalidUserError,
    MissingClaimError,
    MissingUserError,
    MisusedRegistrationToken,
    MisusedResetToken,
    BeskarError,
    LegacyScheme,
    TOTPRequired,
)
from sanic_beskar.constants import (
    AccessType,
    DEFAULT_TOKEN_ACCESS_LIFESPAN,
    DEFAULT_TOKEN_REFRESH_LIFESPAN,
    DEFAULT_TOKEN_HEADER_NAME,
    DEFAULT_TOKEN_HEADER_TYPE,
    IS_REGISTRATION_TOKEN_CLAIM,
    IS_RESET_TOKEN_CLAIM,
    REFRESH_EXPIRATION_CLAIM,
    VITAM_AETERNUM,
)


class TestBeskar:
    async def test_hash_password(self, default_guard):
        """
        This test verifies that Beskar hashes passwords using the scheme
        specified by the HASH_SCHEME setting. If no scheme is supplied, the
        test verifies that the default scheme is used. Otherwise, the test
        verifies that the hashed password matches the supplied scheme.
        """
        secret = default_guard.hash_password("some password")
        assert default_guard.pwd_ctx.identify(secret) == "pbkdf2_sha512"

    async def test__verify_password(self, app, user_class, default_guard):
        """
        This test verifies that the _verify_password function can be used to
        successfully compare a raw password against its hashed version
        """
        secret = default_guard.hash_password("some password")
        assert default_guard._verify_password("some password", secret)
        assert not default_guard._verify_password("not right", secret)

        app.config["BESKAR_HASH_SCHEME"] = "pbkdf2_sha512"
        specified_guard = Beskar(app, user_class)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            specified_guard.pwd_ctx.update(pbkdf2_sha512__default_rounds=1)
        secret = specified_guard.hash_password("some password")
        assert specified_guard._verify_password("some password", secret)
        assert not specified_guard._verify_password("not right", secret)

    async def test_authenticate(self, user_class, default_guard, mock_users):
        """
        This test verifies that the authenticate function can be used to
        retrieve a User instance when the correct username and password are
        supplied. It also verifies that an AuthenticationError is raised
        when a valid user/password combination are not supplied.
        """
        the_dude = await mock_users(username='the_dude', password='abides')

        loaded_user = await user_class.lookup(username=the_dude.username)
        authed_user = await default_guard.authenticate("the_dude", "abides")

        assert loaded_user.id == authed_user.id
        assert loaded_user.password == authed_user.password

        with pytest.raises(AuthenticationError):
            await default_guard.authenticate("the_bro", "abides")
        with pytest.raises(AuthenticationError):
            await default_guard.authenticate("the_dude", "is_undudelike")

        await the_dude.delete()

    def test__validate_user_class__success_with_valid_user_class(
        self,
        user_class,
        default_guard,
    ):
        assert default_guard._validate_user_class(user_class)

    def test__validate_user_class__fails_if_class_has_no_lookup_classmethod(
        self,
        default_guard,
    ):
        class NoLookupUser:
            @classmethod
            def identify(cls, id):
                pass

        with pytest.raises(BeskarError) as err_info:
            default_guard._validate_user_class(NoLookupUser)
        assert "must have a lookup class method" in err_info.value.message

    def test__validate_user_class__fails_if_class_has_no_identify_classmethod(
        self,
        default_guard,
    ):
        class NoIdentifyUser:
            @classmethod
            def lookup(cls, username):
                pass

        with pytest.raises(BeskarError) as err_info:
            default_guard._validate_user_class(NoIdentifyUser)
        assert "must have an identify class method" in err_info.value.message

    def test__validate_user_class__fails_if_class_has_no_identity_attribute(
        self,
        default_guard,
    ):
        class NoIdentityUser:
            rolenames = []
            password = ""

            @classmethod
            def identify(cls, id):
                pass

            @classmethod
            def lookup(cls, username):
                pass

        with pytest.raises(BeskarError) as err_info:
            default_guard._validate_user_class(NoIdentityUser)
        assert "must have an identity attribute" in err_info.value.message

    def test__validate_user_class__fails_if_class_has_no_rolenames_attribute(
        self,
        default_guard,
    ):
        class NoRolenamesUser:
            identity = 0
            password = ""

            @classmethod
            def identify(cls, id):
                pass

            @classmethod
            def lookup(cls, username):
                pass

        with pytest.raises(BeskarError) as err_info:
            default_guard._validate_user_class(NoRolenamesUser)
        assert "must have a rolenames attribute" in err_info.value.message

    def test__validate_user_class__skips_rolenames_check_if_roles_are_disabled(
        self,
        app,
        user_class,
    ):
        class NoRolenamesUser:
            identity = 0
            password = ""

            @classmethod
            def identify(cls, id):
                pass

            @classmethod
            def lookup(cls, username):
                pass

        app.config["BESKAR_ROLES_DISABLED"] = True
        guard = Beskar(app, user_class)
        assert guard._validate_user_class(NoRolenamesUser)

    def test__validate_user_class__fails_if_class_has_no_password_attribute(
        self,
        default_guard,
    ):
        class NoPasswordUser:
            identity = 0
            rolenames = []

            @classmethod
            def identify(cls, id):
                pass

            @classmethod
            def lookup(cls, username):
                pass

        with pytest.raises(BeskarError) as err_info:
            default_guard._validate_user_class(NoPasswordUser)
        assert "must have a password attribute" in err_info.value.message

    def test__validate_user_class__skips_inst_check_if_constructor_req_params(
        self,
        default_guard,
    ):
        class EmptyInitBlowsUpUser:
            def __init__(self, *args):
                BeskarError.require_condition(len(args) > 0, "BOOM")

            @classmethod
            def identify(cls, id):
                pass

            @classmethod
            def lookup(cls, username):
                pass

        assert default_guard._validate_user_class(EmptyInitBlowsUpUser)

    def test__validate_token_data__fails_when_missing_jti(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = dict()
        with pytest.raises(MissingClaimError) as err_info:
            guard._validate_token_data(data, AccessType.access)
        assert "missing jti" in str(err_info.value)

    def test__validate_token_data__fails_when_jit_is_blacklisted(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class, is_blacklisted=(lambda jti: True))
        data = dict(jti="jti")
        with pytest.raises(BlacklistedError):
            guard._validate_token_data(data, AccessType.access)

    def test__validate_token_data__fails_when_id_is_missing(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = dict(jti="jti")
        with pytest.raises(MissingClaimError) as err_info:
            guard._validate_token_data(data, AccessType.access)
        assert "missing id" in str(err_info.value)

    def test__validate_token_data__fails_when_exp_is_missing(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = dict(jti="jti", id=1)
        with pytest.raises(MissingClaimError) as err_info:
            guard._validate_token_data(data, AccessType.access)
        assert "missing exp" in str(err_info.value)

    def test__validate_token_data__fails_when_refresh_is_missing(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
        }
        with pytest.raises(MissingClaimError) as err_info:
            guard._validate_token_data(data, AccessType.access)
        assert "missing {}".format(REFRESH_EXPIRATION_CLAIM) in str(
            err_info.value
        )

    def test__validate_token_data__fails_when_access_has_expired(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
        }
        with plummet.frozen_time('2017-05-21 19:54:32'):
            with pytest.raises(ExpiredAccessError):
                guard._validate_token_data(data, AccessType.access)

    def test__validate_token_data__fails_on_early_refresh(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
        }
        with plummet.frozen_time('2017-05-21 19:54:28'):
            with pytest.raises(EarlyRefreshError):
                guard._validate_token_data(data, AccessType.refresh)

    def test__validate_token_data__fails_when_refresh_has_expired(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
        }
        with plummet.frozen_time('2017-05-21 20:54:32'):
            with pytest.raises(ExpiredRefreshError):
                guard._validate_token_data(data, AccessType.refresh)

    def test__validate_token_data__fails_on_access_with_register_claim(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
            IS_REGISTRATION_TOKEN_CLAIM: True,
        }
        with plummet.frozen_time('2017-05-21 19:54:28'):
            with pytest.raises(MisusedRegistrationToken):
                guard._validate_token_data(data, AccessType.access)

    def test__validate_token_data__fails_on_refresh_with_register_claim(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
            IS_REGISTRATION_TOKEN_CLAIM: True,
        }
        with plummet.frozen_time('2017-05-21 19:54:32'):
            with pytest.raises(MisusedRegistrationToken):
                guard._validate_token_data(data, AccessType.refresh)

    def test__validate_token_data__fails_on_access_with_reset_claim(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
            IS_RESET_TOKEN_CLAIM: True,
        }
        with plummet.frozen_time('2017-05-21 19:54:28'):
            with pytest.raises(MisusedResetToken):
                guard._validate_token_data(data, AccessType.access)

    def test__validate_token_data__succeeds_with_valid_token(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
        }
        with plummet.frozen_time('2017-05-21 19:54:28'):
            guard._validate_token_data(data, AccessType.access)

    def test__validate_token_data__succeeds_when_refreshing(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
        }
        with plummet.frozen_time('2017-05-21 19:54:32'):
            guard._validate_token_data(data, AccessType.refresh)

    def test__validate_token_data__succeeds_when_registering(
        self,
        app,
        user_class,
    ):
        guard = Beskar(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
            IS_REGISTRATION_TOKEN_CLAIM: True,
        }
        with plummet.frozen_time('2017-05-21 19:54:28'):
            guard._validate_token_data(data, AccessType.register)

    async def test_encode_token(self, app, validating_user_class, mock_users, default_guard, no_token_validation):
        """
        This test::
            * verifies that the encode_token correctly encodes token
              data based on a user instance.
            * verifies that if a user specifies an override for the access
              lifespan it is used in lieu of the instance's access_lifespan.
            * verifies that the access_lifespan cannot exceed the refresh
              lifespan.
            * ensures that if the user_class has the instance method
              validate(), it is called an any exceptions it raises are wrapped
              in an InvalidUserError
            * verifies that custom claims may be encoded in the token and
              validates that the custom claims do not collide with reserved
              claims
        """
        the_dude = await mock_users(username="the_dude", password="abides", roles="admin,operator")
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            logger.critical(f"Token Type: {default_guard.token_provider}")
            token = await default_guard.encode_token(the_dude)
            token_data = await default_guard.extract_token(token)

            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + DEFAULT_TOKEN_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_TOKEN_REFRESH_LIFESPAN).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"

        override_access_lifespan = pendulum.Duration(minutes=1)
        override_refresh_lifespan = pendulum.Duration(hours=1)
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await default_guard.encode_token(
                the_dude,
                override_access_lifespan=override_access_lifespan,
                override_refresh_lifespan=override_refresh_lifespan,
            )
            token_data = await default_guard.extract_token(token)

            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + override_access_lifespan).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + override_refresh_lifespan).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"

        override_access_lifespan = pendulum.Duration(hours=1)
        override_refresh_lifespan = pendulum.Duration(minutes=1)
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await default_guard.encode_token(
                the_dude,
                override_access_lifespan=override_access_lifespan,
                override_refresh_lifespan=override_refresh_lifespan,
            )
            token_data = await default_guard.extract_token(token)
            assert token_data["iat"] == moment.int_timestamp
            assert token_data["exp"] == token_data[REFRESH_EXPIRATION_CLAIM]
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + override_refresh_lifespan).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"

        validating_guard = Beskar(app, validating_user_class)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            validating_guard.pwd_ctx.update(pbkdf2_sha512__default_rounds=1)
        brandt = validating_user_class(
            username="brandt",
            password=validating_guard.hash_password("can't watch"),
            is_active=True,
        )
        await validating_guard.encode_token(brandt)
        brandt.is_active = False
        with pytest.raises(InvalidUserError) as err_info:
            await validating_guard.encode_token(brandt)
        expected_message = "The user is not valid or has had access revoked"
        assert expected_message in str(err_info.value)

        moment = plummet.momentize('2018-08-18 08:55:12')
        with plummet.frozen_time(moment):
            token = await default_guard.encode_token(
                the_dude,
                duder="brief",
                el_duderino="not brief",
            )
            token_data = await default_guard.extract_token(token)
            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + DEFAULT_TOKEN_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_TOKEN_REFRESH_LIFESPAN).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"
            assert token_data["duder"] == "brief"
            assert token_data["el_duderino"] == "not brief"

        with pytest.raises(ClaimCollisionError) as err_info:
            await default_guard.encode_token(the_dude, exp="nice marmot")
        expected_message = "custom claims collide"
        assert expected_message in str(err_info.value)

    async def test_encode_eternal_token(self, mock_users, no_token_validation, default_guard):
        """
        This test verifies that the encode_eternal_token correctly encodes
        token data based on a user instance. Also verifies that the lifespan is
        set to the constant VITAM_AETERNUM
        """
        the_dude = await mock_users(username='the_dude', roles="admin,operator")
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await default_guard.encode_eternal_token(the_dude)
            token_data = await default_guard.extract_token(token)
            assert token_data["iat"] == moment.int_timestamp
            assert token_data["exp"] == (moment + VITAM_AETERNUM).int_timestamp
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + VITAM_AETERNUM).int_timestamp
            )
            assert token_data["id"] == the_dude.id

    async def test_refresh_token(
        self,
        app,
        validating_user_class,
        mock_users,
        default_guard,
    ):
        """
        This test::
            * verifies that the refresh_token properly generates
              a refreshed token.
            * ensures that a token who's access permission has not expired may
              not be refreshed.
            * ensures that a token who's access permission has expired must not
              have an expired refresh permission for a new token to be issued.
            * ensures that if an override_access_lifespan argument is supplied
              that it is used instead of the instance's access_lifespan.
            * ensures that the access_lifespan may not exceed the refresh
              lifespan.
            * ensures that if the user_class has the instance method
              validate(), it is called an any exceptions it raises are wrapped
              in an InvalidUserError.
            * verifies that if a user is no longer identifiable that a
              MissingUserError is raised
            * verifies that any custom claims in the original token's
              payload are also packaged in the new token's payload
        """

        the_dude = await mock_users(username="the_dude",
                                    password="abides",
                                    roles="admin,operator")

        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await default_guard.encode_token(the_dude)
        new_moment = (
            pendulum.parse("2017-05-21 18:39:55")
            + DEFAULT_TOKEN_ACCESS_LIFESPAN
            + pendulum.Duration(minutes=1)
        )
        with plummet.frozen_time(new_moment):
            new_token = await default_guard.refresh_token(token)
            new_token_data = await default_guard.extract_token(new_token)
            assert new_token_data["iat"] == new_moment.int_timestamp
            assert (
                new_token_data["exp"]
                == (new_moment + DEFAULT_TOKEN_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                new_token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_TOKEN_REFRESH_LIFESPAN).int_timestamp
            )
            assert new_token_data["id"] == the_dude.id
            assert new_token_data["rls"] == "admin,operator"

        moment = plummet.momentize("2017-05-21 18:39:55")
        with plummet.frozen_time('2017-05-21 18:39:55'):
            token = await default_guard.encode_token(the_dude)
        new_moment = (
            pendulum.parse("2017-05-21 18:39:55")
            + DEFAULT_TOKEN_ACCESS_LIFESPAN
            + pendulum.Duration(minutes=1)
        )
        with plummet.frozen_time(new_moment):
            new_token = await default_guard.refresh_token(
                token,
                override_access_lifespan=pendulum.Duration(hours=2),
            )
            new_token_data = await default_guard.extract_token(new_token)
            assert (
                new_token_data["exp"]
                == (new_moment + pendulum.Duration(hours=2)).int_timestamp
            )

        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await default_guard.encode_token(
                the_dude,
                override_refresh_lifespan=pendulum.Duration(hours=2),
                override_access_lifespan=pendulum.Duration(minutes=30),
            )
        new_moment = moment + pendulum.Duration(minutes=31)
        with plummet.frozen_time(new_moment):
            new_token = await default_guard.refresh_token(
                token,
                override_access_lifespan=pendulum.Duration(hours=2),
            )
            logger.critical(f'new_token: {new_token}')
            new_token_data = await default_guard.extract_token(new_token)
            logger.critical(f"new_token_data: {new_token_data}")
            assert (
                new_token_data["exp"]
                == new_token_data[REFRESH_EXPIRATION_CLAIM]
            )

        expiring_interval = DEFAULT_TOKEN_ACCESS_LIFESPAN + pendulum.Duration(
            minutes=1
        )

        validating_guard = Beskar(app, validating_user_class)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            validating_guard.pwd_ctx.update(pkdbf2_sha512__default_rounds=1)
        brandt = await mock_users(username="brandt",
                                  password="can't watch",
                                  guard_name=validating_guard,
                                  class_name=validating_user_class)
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await validating_guard.encode_token(brandt)
        new_moment = moment + expiring_interval
        with plummet.frozen_time(new_moment):
            await validating_guard.refresh_token(token)
        brandt.is_active = False
        await brandt.save(update_fields=["is_active"])
        new_moment = new_moment + expiring_interval
        with plummet.frozen_time(new_moment):
            with pytest.raises(InvalidUserError) as err_info:
                await validating_guard.refresh_token(token)
        expected_message = "The user is not valid or has had access revoked"
        assert expected_message in str(err_info.value)

        expiring_interval = DEFAULT_TOKEN_ACCESS_LIFESPAN + pendulum.Duration(
            minutes=1
        )

        bunny = await mock_users(username="bunny", guard_name=default_guard)
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await default_guard.encode_token(bunny)
        await bunny.delete()
        new_moment = moment + expiring_interval
        with plummet.frozen_time(new_moment):
            with pytest.raises(MissingUserError) as err_info:
                await validating_guard.refresh_token(token)
        expected_message = "Could not find the requested user"
        assert expected_message in str(err_info.value)

        moment = plummet.momentize('2018-08-14 09:05:24')
        with plummet.frozen_time(moment):
            token = await default_guard.encode_token(
                the_dude,
                duder="brief",
                el_duderino="not brief",
            )
        new_moment = (
            pendulum.parse("2018-08-14 09:05:24")
            + DEFAULT_TOKEN_ACCESS_LIFESPAN
            + pendulum.Duration(minutes=1)
        )
        with plummet.frozen_time(new_moment):
            new_token = await default_guard.refresh_token(token)
            new_token_data = await default_guard.extract_token(new_token)
            assert new_token_data["iat"] == new_moment.int_timestamp
            assert (
                new_token_data["exp"]
                == (new_moment + DEFAULT_TOKEN_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                new_token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_TOKEN_REFRESH_LIFESPAN).int_timestamp
            )
            assert new_token_data["id"] == the_dude.id
            assert new_token_data["rls"] == "admin,operator"
            assert new_token_data["duder"] == "brief"
            assert new_token_data["el_duderino"] == "not brief"

        await the_dude.delete()
        await brandt.delete()
        await bunny.delete()

    async def test_read_token_from_header(self, client, mock_users, default_guard):
        """
        This test verifies that a token may be properly read from a flask
        request's header using the configuration settings for header name and
        type
        """
        the_dude = await mock_users(username='the_dude', password='abides', roles='admin,operator')

        with plummet.frozen_time('2017-05-21 18:39:55'):
            token = await default_guard.encode_token(the_dude)
            logger.critical(f'Token: {token}')

        request, _ = client.get(
            "/unprotected",
            headers={
                "Content-Type": "application/json",
                DEFAULT_TOKEN_HEADER_NAME: DEFAULT_TOKEN_HEADER_TYPE + " " + token,
            },
        )

        assert default_guard.read_token_from_header(request) == token
        assert default_guard.read_token(request) == token
        await the_dude.delete()

    async def test_read_token_from_cookie(
        self, client, mock_users, default_guard
    ):
        """
        This test verifies that a token may be properly read from a flask
        request's cookies using the configuration settings for cookie
        """
        the_dude = await mock_users(username='the_dude', roles='admin,operator')

        cookies = Cookies()
        with plummet.frozen_time('2017-05-21 18:39:55'):
            token = await default_guard.encode_token(the_dude)
            cookies[default_guard.cookie_name] = token
            request, _ = client.get(
                "/unprotected",
                cookies=cookies
            )

        assert default_guard.read_token_from_cookie(request) == token
        assert default_guard.read_token(request) == token

        await the_dude.delete()

    async def test_pack_header_for_user(self, mock_users, no_token_validation, default_guard):
        """
        This test::
          * verifies that the pack_header_for_user method can be used to
            package a token into a header dict for a specified user
          * verifies that custom claims may be packaged as well
        """
        the_dude = await mock_users(username='the_dude', roles='admin,operator')

        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            header_dict = await default_guard.pack_header_for_user(the_dude)
            token_header = header_dict.get(DEFAULT_TOKEN_HEADER_NAME)
            assert token_header is not None
            token = token_header.replace(DEFAULT_TOKEN_HEADER_TYPE, "")
            token = token.strip()
            token_data = await default_guard.extract_token(token)
            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + DEFAULT_TOKEN_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_TOKEN_REFRESH_LIFESPAN).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"

        moment = plummet.momentize('2017-05-21 18:39:55')
        override_access_lifespan = pendulum.Duration(minutes=1)
        override_refresh_lifespan = pendulum.Duration(hours=1)
        with plummet.frozen_time(moment):
            header_dict = await default_guard.pack_header_for_user(
                the_dude,
                override_access_lifespan=override_access_lifespan,
                override_refresh_lifespan=override_refresh_lifespan,
            )
            token_header = header_dict.get(DEFAULT_TOKEN_HEADER_NAME)
            assert token_header is not None
            token = token_header.replace(DEFAULT_TOKEN_HEADER_TYPE, "")
            token = token.strip()
            token_data = await default_guard.extract_token(token)
            assert (
                token_data["exp"]
                == (moment + override_access_lifespan).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + override_refresh_lifespan).int_timestamp
            )
            assert token_data["id"] == the_dude.id

        moment = plummet.momentize('2018-08-14 09:08:39')
        with plummet.frozen_time(moment):
            header_dict = await default_guard.pack_header_for_user(
                the_dude,
                duder="brief",
                el_duderino="not brief",
            )
            token_header = header_dict.get(DEFAULT_TOKEN_HEADER_NAME)
            assert token_header is not None
            token = token_header.replace(DEFAULT_TOKEN_HEADER_TYPE, "")
            token = token.strip()
            token_data = await default_guard.extract_token(token)
            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + DEFAULT_TOKEN_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_TOKEN_REFRESH_LIFESPAN).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"
            assert token_data["duder"] == "brief"
            assert token_data["el_duderino"] == "not brief"

    async def test_reset_email(self, app, tmpdir, default_guard, mock_users):
        """
        This test verifies email based password reset functions as expected.
        This includes sending messages with valid time expiring tokens
           and ensuring the body matches the expected body, as well
           as token validation.
        """
        template = """
            <!doctype html>
            <html>
              <head><title>Reset Password</title></head>
              <body>{{ token }}</body>
            </html>
        """
        template_file = tmpdir.join("test_template.html")
        template_file.write(template)

        app.config["TESTING"] = True
        app.config["BESKAR_EMAIL_TEMPLATE"] = str(template_file)
        app.config["BESKAR_RESET_ENDPOINT"] = "unprotected"

        # create our default test user
        the_dude = await mock_users(username='the_dude', password='blah')

        # test a bad username
        with pytest.raises(MissingUserError):
            notify = await default_guard.send_reset_email(
                email="fail@whale.org",
                reset_sender="you@whatever.com",
            )

        # test a good username
        notify = await default_guard.send_reset_email(
            email=the_dude.email,
            reset_sender="you@whatever.com",
        )
        token = notify["token"]

        # test our own interpretation and what we got back from flask_mail
        assert token in notify["message"]
        assert not notify["result"]

        # test our token is good
        token_data = await default_guard.extract_token(
            notify["token"],
            access_type=AccessType.reset,
        )
        assert token_data[IS_RESET_TOKEN_CLAIM]

        validated_user = await default_guard.validate_reset_token(token)
        assert validated_user == the_dude

        await the_dude.delete()

    async def test_registration_email(
        self,
        app,
        tmpdir,
        default_guard,
        mock_users,
    ):
        """
        This test verifies email based registration functions as expected.
        This includes sending messages with valid time expiring tokens
           and ensuring the body matches the expected body, as well
           as token validation.
        """
        template = """
            <!doctype html>
            <html>
              <head><title>Email Verification</title></head>
              <body>{{ token }}</body>
            </html>
        """
        template_file = tmpdir.join("test_template.html")
        template_file.write(template)

        app.config["TESTING"] = True
        app.config["BESKAR_EMAIL_TEMPLATE"] = str(template_file)
        app.config["BESKAR_CONFIRMATION_ENDPOINT"] = "unprotected"

        # create our default test user
        the_dude = await mock_users(username='the_dude', password='Abides')

        notify = await default_guard.send_registration_email(
            "the@dude.com",
            user=the_dude,
            confirmation_sender="you@whatever.com",
        )
        token = notify["token"]

        # test our own interpretation and what we got back from flask_mail
        assert token in notify["message"]
        assert not notify["result"]

        # test our token is good
        token_data = await default_guard.extract_token(
            notify["token"],
            access_type=AccessType.register,
        )
        assert token_data[IS_REGISTRATION_TOKEN_CLAIM]

    async def test_get_user_from_registration_token(
        self,
        default_guard,
        mock_users,
    ):
        """
        This test verifies that a user can be extracted from an email based
        registration token. Also verifies that a token that has expired
        cannot be used to fetch a user. Also verifies that a registration
        token may not be refreshed
        """
        # create our default test user
        the_dude = await mock_users(username='the_dude')

        reg_token = await default_guard.encode_token(
            the_dude,
            bypass_user_check=True,
            is_registration_token=True,
        )
        extracted_user = await default_guard.get_user_from_registration_token(
            reg_token
        )
        assert extracted_user == the_dude

        """
           test to ensure a registration token that is expired
               sets off an 'ExpiredAccessError' exception
        """
        with plummet.frozen_time('2019-01-30 16:30:00'):
            expired_reg_token = await default_guard.encode_token(
                the_dude,
                bypass_user_check=True,
                override_access_lifespan=pendulum.Duration(minutes=1),
                is_registration_token=True,
            )

        with plummet.frozen_time('2019-01-30 16:40:00'):
            with pytest.raises(ExpiredAccessError):
                await default_guard.get_user_from_registration_token(
                    expired_reg_token
                )

    async def test_validate_and_update(self, app, user_class, default_guard, mock_users):
        """
        This test verifies that Beskar hashes passwords using the scheme
        specified by the HASH_SCHEME setting. If no scheme is supplied, the
        test verifies that the default scheme is used. Otherwise, the test
        verifies that the hashed password matches the supplied scheme.
        """
        pbkdf2_sha512_password = default_guard.hash_password("pbkdf2_sha512")

        # create our default test user
        the_dude = await mock_users(username='the_dude', password=pbkdf2_sha512_password)

        """
        Test the current password is hashed with BESKAR_HASH_SCHEME
        """
        assert await default_guard.verify_and_update(user=the_dude)

        """
        Test a password hashed with something other than
            BESKAR_HASH_ALLOWED_SCHEME triggers an Exception.
        """
        app.config["BESKAR_HASH_SCHEME"] = "bcrypt"
        default_guard = Beskar(app, user_class)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            default_guard.pwd_ctx.update(bcrypt__default_rounds=1)
        bcrypt_password = default_guard.hash_password("bcrypt_password")
        the_dude.password = bcrypt_password

        del app.config["BESKAR_HASH_SCHEME"]
        app.config["BESKAR_HASH_DEPRECATED_SCHEMES"] = ["bcrypt"]
        default_guard = Beskar(app, user_class)
        with pytest.raises(LegacyScheme):
            await default_guard.verify_and_update(the_dude)

        """
        Test a password hashed with something other than
            BESKAR_HASH_SCHEME, and supplied good password
            gets the user entry's password updated and saved.
        """
        the_dude_old_password = the_dude.password
        updated_dude = await default_guard.verify_and_update(
            the_dude, "bcrypt_password"
        )
        assert updated_dude.password != the_dude_old_password

        """
        Test a password hashed with something other than
            BESKAR_HASH_SCHEME, and supplied bad password
            gets an Exception raised.
        """
        the_dude.password = bcrypt_password
        with pytest.raises(AuthenticationError):
            await default_guard.verify_and_update(user=the_dude, password="failme")

        # put away your toys
        await the_dude.delete()

    async def test_authenticate_validate_and_update(self, app, user_class, mock_users, default_guard):
        """
        This test verifies the authenticate() function, when altered by
        either 'BESKAR_HASH_AUTOUPDATE' or 'BESKAR_HASH_AUTOTEST'
        performs the authentication and the required subaction.
        """

        pbkdf2_sha512_password = default_guard.hash_password("start_password")

        # create our default test user
        the_dude = await mock_users(username='fuckyou', email="fuck@you.com", password=pbkdf2_sha512_password)

        """
        Test the existing model as a baseline
        """
        assert await default_guard.authenticate(the_dude.username, "start_password")

        """
        Test the existing model with a bad password as a baseline
        """
        with pytest.raises(AuthenticationError):
            await default_guard.authenticate(the_dude.username, "failme")

        """
        Test the updated model with a bad hash scheme and AUTOTEST enabled.
        Should raise and exception
        """
        app.config["BESKAR_HASH_SCHEME"] = "bcrypt"
        default_guard = Beskar(app, user_class)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            default_guard.pwd_ctx.update(bcrypt__default_rounds=1)
        bcrypt_password = default_guard.hash_password("bcrypt_password")
        the_dude.password = bcrypt_password
        await the_dude.save(update_fields=["password"])

        del app.config["BESKAR_HASH_SCHEME"]
        app.config["BESKAR_HASH_DEPRECATED_SCHEMES"] = ["bcrypt"]
        app.config["BESKAR_HASH_AUTOTEST"] = True
        default_guard = Beskar(app, user_class)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            default_guard.pwd_ctx.update(bcrypt__default_rounds=1)
        with pytest.raises(LegacyScheme):
            await default_guard.authenticate(the_dude.username, "bcrypt_password")

        """
        Test the updated model with a bad hash scheme and AUTOUPDATE enabled.
        Should return an updated user object we need to save ourselves.
        """
        the_dude_old_password = the_dude.password
        app.config["BESKAR_HASH_AUTOUPDATE"] = True
        default_guard = Beskar(app, user_class)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            default_guard.pwd_ctx.update(pbkdf2_sha512__default_rounds=1)
        updated_dude = await default_guard.authenticate(
            the_dude.username, "bcrypt_password"
        )
        assert updated_dude.password != the_dude_old_password

        # put away your toys
        await the_dude.delete()

    async def test_totp(self, app, user_class, totp_user_class, mock_users, default_guard):
        """
        This test verifies the authenticate_totp() function, for use
        with TOTP two factor authentication.
        """

        totp_guard = Beskar(app, totp_user_class)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            totp_guard.pwd_ctx.update(pbkdf2_sha512__default_rounds=1)
        totp = totp_guard.totp_ctx.new()

        # create our default test user
        the_dude = await mock_users(username='the_dude',
                                    password='abides',
                                    class_name=totp_user_class,
                                    guard_name=totp_guard,
                                    totp=totp.to_json())

        # create our default test user
        await mock_users(username='the_muggle',
                         password='human',
                         class_name=user_class,
                         guard_name=default_guard)

        assert the_dude.totp == totp.to_json()

        # create a token for `the_dude`
        token = totp.generate().token
        # verify the token value is good for user `the_dude`
        assert not the_dude.totp_last_counter
        verify_token = totp.verify(token, the_dude.totp, last_counter=the_dude.totp_last_counter)
        the_dude = await totp_guard.authenticate_totp('the_dude', totp.generate().token)

        # test if we are updating our replay prevention cache
        assert verify_token.counter == the_dude.totp_last_counter

        # cleanup and try via `authenticate`
        the_dude.totp_last_counter = None
        await the_dude.save(update_fields=["totp_last_counter"])
        assert not the_dude.totp_last_counter
        the_dude = await totp_guard.authenticate('the_dude', 'abides', totp.generate().token)
        assert the_dude.totp_last_counter

        # verify a proper failure if TOTP not configured for user
        with pytest.raises(AuthenticationError, match=r'TOTP challenge is not properly configured'):
            await default_guard.authenticate_totp('the_muggle', 80085)
        with pytest.raises(AuthenticationError, match=r'TOTP challenge is not properly configured'):
            await default_guard.authenticate('the_muggle', 'human', 80085)

        # verify a replay failure
        with pytest.raises(UsedTokenError):
            totp.verify(token, the_dude.totp, last_counter=the_dude.totp_last_counter)
        with pytest.raises(UsedTokenError):
            await totp_guard.authenticate_totp('the_dude', token)
        with pytest.raises(UsedTokenError):
            await totp_guard.authenticate('the_dude', 'abides', token)

        # verify a bad token failure
        with pytest.raises(InvalidTokenError):
            totp.verify(313373, the_dude.totp, last_counter=the_dude.totp_last_counter)
        with pytest.raises(InvalidTokenError):
            await totp_guard.authenticate_totp('the_dude', 313373)
        with pytest.raises(InvalidTokenError):
            await totp_guard.authenticate('the_dude', 'abides', 313373)

        # verify a missing token failure
        with pytest.raises(AuthenticationError) as e:
            await totp_guard.authenticate_totp('the_dude', None)  # Null token provided
        with pytest.raises(AuthenticationError) as e:
            await totp_guard.authenticate('the_dude', 'abides', None)  # No token provided
        # the `authenticate` for a TOTP user, not providing `token` is a special return
        assert e.type == TOTPRequired

        # verify an invalid format token failure
        with pytest.raises(MalformedTokenError):
            totp.verify(8008135, the_dude.totp, last_counter=the_dude.totp_last_counter)
        with pytest.raises(MalformedTokenError):
            await totp_guard.authenticate_totp('the_dude', 8008135)
        with pytest.raises(MalformedTokenError):
            await totp_guard.authenticate('the_dude', 'abides', 8008135)

        app.config.BESKAR_TOTP_SECRETS_TYPE = 'failwhale'
        with pytest.raises(BeskarError):
            Beskar(app, totp_user_class)

        app.config.BESKAR_TOTP_SECRETS_TYPE = 'string'
        app.config.BESKAR_TOTP_SECRETS_DATA = {1: generate_secret()}
        totp_protected_guard = Beskar(app, totp_user_class)
        totp_protected = totp_protected_guard.totp_ctx.new()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            totp_protected_guard.pwd_ctx.update(pbkdf2_sha512__default_rounds=1)

        # create our default test user w/ encrypted TOTP
        the_protected_dude = await mock_users(username='the_protected_dude',
                                              password='abides',
                                              class_name=totp_user_class,
                                              guard_name=totp_protected_guard,
                                              totp=totp_protected.to_json())

        # ensure we can load the output as json
        the_protected_dude_totp = ujson.loads(the_protected_dude.totp)
        # ensure the key is encrypted
        assert the_protected_dude_totp.get('enckey')
        # put away your toys
        await the_dude.delete()
