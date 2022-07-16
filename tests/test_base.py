import jwt
import pendulum
import plummet
import pytest

from httpx import Cookies

from sanic.log import logger
from sanic_testing.reusable import ReusableClient

from models import User

from sanic_praetorian import Praetorian
from sanic_praetorian.exceptions import (
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
    PraetorianError,
    LegacyScheme,
)
from sanic_praetorian.constants import (
    AccessType,
    DEFAULT_JWT_ACCESS_LIFESPAN,
    DEFAULT_JWT_REFRESH_LIFESPAN,
    DEFAULT_JWT_HEADER_NAME,
    DEFAULT_JWT_HEADER_TYPE,
    IS_REGISTRATION_TOKEN_CLAIM,
    IS_RESET_TOKEN_CLAIM,
    REFRESH_EXPIRATION_CLAIM,
    VITAM_AETERNUM,
)


class TestPraetorian:
    async def test_hash_password(self, app, user_class, default_guard):
        """
        This test verifies that Praetorian hashes passwords using the scheme
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

        app.config["PRAETORIAN_HASH_SCHEME"] = "pbkdf2_sha512"
        specified_guard = Praetorian(app, user_class)
        secret = specified_guard.hash_password("some password")
        assert specified_guard._verify_password("some password", secret)
        assert not specified_guard._verify_password("not right", secret)

    async def test_authenticate(self, app, user_class, default_guard):
        """
        This test verifies that the authenticate function can be used to
        retrieve a User instance when the correct username and password are
        supplied. It also verifies that an AuthenticationError is raised
        when a valid user/password combination are not supplied.
        """
        the_dude = await user_class.create(username="TheDude",
                                           password=default_guard.hash_password("abides"),
                                           email='thedude@foo.com', roles="")

        loaded_user = await user_class.lookup(username=the_dude.username)
        authed_user = await default_guard.authenticate("TheDude", "abides")

        assert loaded_user.id == authed_user.id
        assert loaded_user.password == authed_user.password

        with pytest.raises(AuthenticationError):
            await default_guard.authenticate("TheBro", "abides")
        with pytest.raises(AuthenticationError):
            await default_guard.authenticate("TheDude", "is_undudelike")

        await the_dude.delete()

    def test__validate_user_class__success_with_valid_user_class(
        self,
        app,
        user_class,
        default_guard,
    ):
        assert default_guard._validate_user_class(user_class)

    def test__validate_user_class__fails_if_class_has_no_lookup_classmethod(
        self,
        app,
        default_guard,
    ):
        class NoLookupUser:
            @classmethod
            def identify(cls, id):
                pass

        with pytest.raises(PraetorianError) as err_info:
            default_guard._validate_user_class(NoLookupUser)
        assert "must have a lookup class method" in err_info.value.message

    def test__validate_user_class__fails_if_class_has_no_identify_classmethod(
        self,
        app,
        default_guard,
    ):
        class NoIdentifyUser:
            @classmethod
            def lookup(cls, username):
                pass

        with pytest.raises(PraetorianError) as err_info:
            default_guard._validate_user_class(NoIdentifyUser)
        assert "must have an identify class method" in err_info.value.message

    def test__validate_user_class__fails_if_class_has_no_identity_attribute(
        self,
        app,
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

        with pytest.raises(PraetorianError) as err_info:
            default_guard._validate_user_class(NoIdentityUser)
        assert "must have an identity attribute" in err_info.value.message

    def test__validate_user_class__fails_if_class_has_no_rolenames_attribute(
        self,
        app,
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

        with pytest.raises(PraetorianError) as err_info:
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

        app.config["PRAETORIAN_ROLES_DISABLED"] = True
        guard = Praetorian(app, user_class)
        assert guard._validate_user_class(NoRolenamesUser)

    def test__validate_user_class__fails_if_class_has_no_password_attribute(
        self,
        app,
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

        with pytest.raises(PraetorianError) as err_info:
            default_guard._validate_user_class(NoPasswordUser)
        assert "must have a password attribute" in err_info.value.message

    def test__validate_user_class__skips_inst_check_if_constructor_req_params(
        self,
        app,
        default_guard,
    ):
        class EmptyInitBlowsUpUser:
            def __init__(self, *args):
                PraetorianError.require_condition(len(args) > 0, "BOOM")

            @classmethod
            def identify(cls, id):
                pass

            @classmethod
            def lookup(cls, username):
                pass

        assert default_guard._validate_user_class(EmptyInitBlowsUpUser)

    def test__validate_jwt_data__fails_when_missing_jti(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
        data = dict()
        with pytest.raises(MissingClaimError) as err_info:
            guard._validate_jwt_data(data, AccessType.access)
        assert "missing jti" in str(err_info.value)

    def test__validate_jwt_data__fails_when_jit_is_blacklisted(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class, is_blacklisted=(lambda jti: True))
        data = dict(jti="jti")
        with pytest.raises(BlacklistedError):
            guard._validate_jwt_data(data, AccessType.access)

    def test__validate_jwt_data__fails_when_id_is_missing(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
        data = dict(jti="jti")
        with pytest.raises(MissingClaimError) as err_info:
            guard._validate_jwt_data(data, AccessType.access)
        assert "missing id" in str(err_info.value)

    def test__validate_jwt_data__fails_when_exp_is_missing(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
        data = dict(jti="jti", id=1)
        with pytest.raises(MissingClaimError) as err_info:
            guard._validate_jwt_data(data, AccessType.access)
        assert "missing exp" in str(err_info.value)

    def test__validate_jwt_data__fails_when_refresh_is_missing(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
        }
        with pytest.raises(MissingClaimError) as err_info:
            guard._validate_jwt_data(data, AccessType.access)
        assert "missing {}".format(REFRESH_EXPIRATION_CLAIM) in str(
            err_info.value
        )

    def test__validate_jwt_data__fails_when_access_has_expired(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
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
                guard._validate_jwt_data(data, AccessType.access)

    def test__validate_jwt_data__fails_on_early_refresh(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
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
                guard._validate_jwt_data(data, AccessType.refresh)

    def test__validate_jwt_data__fails_when_refresh_has_expired(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
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
                guard._validate_jwt_data(data, AccessType.refresh)

    def test__validate_jwt_data__fails_on_access_with_register_claim(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
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
                guard._validate_jwt_data(data, AccessType.access)

    def test__validate_jwt_data__fails_on_refresh_with_register_claim(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
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
                guard._validate_jwt_data(data, AccessType.refresh)

    def test__validate_jwt_data__fails_on_access_with_reset_claim(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
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
                guard._validate_jwt_data(data, AccessType.access)

    def test__validate_jwt_data__succeeds_with_valid_jwt(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
        }
        with plummet.frozen_time('2017-05-21 19:54:28'):
            guard._validate_jwt_data(data, AccessType.access)

    def test__validate_jwt_data__succeeds_when_refreshing(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
        data = {
            "jti": "jti",
            "id": 1,
            "exp": pendulum.parse("2017-05-21 19:54:30").int_timestamp,
            REFRESH_EXPIRATION_CLAIM: pendulum.parse(
                "2017-05-21 20:54:30"
            ).int_timestamp,
        }
        with plummet.frozen_time('2017-05-21 19:54:32'):
            guard._validate_jwt_data(data, AccessType.refresh)

    def test__validate_jwt_data__succeeds_when_registering(
        self,
        app,
        user_class,
    ):
        guard = Praetorian(app, user_class)
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
            guard._validate_jwt_data(data, AccessType.register)

    async def test_encode_jwt_token(self, app, user_class, validating_user_class):
        """
        This test::
            * verifies that the encode_jwt_token correctly encodes jwt
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
        guard = Praetorian(app, user_class)
        the_dude = await user_class.create(
            username="TheDude",
            password=guard.hash_password("abides"),
            email="thedude@foo.com",
            roles="admin,operator",
        )
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await guard.encode_jwt_token(the_dude)
            token_data = jwt.decode(
                token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + DEFAULT_JWT_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_JWT_REFRESH_LIFESPAN).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"

        override_access_lifespan = pendulum.Duration(minutes=1)
        override_refresh_lifespan = pendulum.Duration(hours=1)
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await guard.encode_jwt_token(
                the_dude,
                override_access_lifespan=override_access_lifespan,
                override_refresh_lifespan=override_refresh_lifespan,
            )
            token_data = jwt.decode(
                token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
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
            token = await guard.encode_jwt_token(
                the_dude,
                override_access_lifespan=override_access_lifespan,
                override_refresh_lifespan=override_refresh_lifespan,
            )
            token_data = jwt.decode(
                token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert token_data["iat"] == moment.int_timestamp
            assert token_data["exp"] == token_data[REFRESH_EXPIRATION_CLAIM]
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + override_refresh_lifespan).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"

        validating_guard = Praetorian(app, validating_user_class)
        brandt = validating_user_class(
            username="brandt",
            password=validating_guard.hash_password("can't watch"),
            is_active=True,
        )
        await validating_guard.encode_jwt_token(brandt)
        brandt.is_active = False
        with pytest.raises(InvalidUserError) as err_info:
            await validating_guard.encode_jwt_token(brandt)
        expected_message = "The user is not valid or has had access revoked"
        assert expected_message in str(err_info.value)

        moment = plummet.momentize('2018-08-18 08:55:12')
        with plummet.frozen_time(moment):
            token = await guard.encode_jwt_token(
                the_dude,
                duder="brief",
                el_duderino="not brief",
            )
            token_data = jwt.decode(
                token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + DEFAULT_JWT_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_JWT_REFRESH_LIFESPAN).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"
            assert token_data["duder"] == "brief"
            assert token_data["el_duderino"] == "not brief"

        with pytest.raises(ClaimCollisionError) as err_info:
            await guard.encode_jwt_token(the_dude, exp="nice marmot")
        expected_message = "custom claims collide"
        assert expected_message in str(err_info.value)

    async def test_encode_eternal_jwt_token(self, app, user_class):
        """
        This test verifies that the encode_eternal_jwt_token correctly encodes
        jwt data based on a user instance. Also verifies that the lifespan is
        set to the constant VITAM_AETERNUM
        """
        guard = Praetorian(app, user_class)
        the_dude = await user_class.create(
            username="TheDude",
            password=guard.hash_password("abides"),
            email="thedude@foo.com",
            roles="admin,operator",
        )
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await guard.encode_eternal_jwt_token(the_dude)
            token_data = jwt.decode(
                token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert token_data["iat"] == moment.int_timestamp
            assert token_data["exp"] == (moment + VITAM_AETERNUM).int_timestamp
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + VITAM_AETERNUM).int_timestamp
            )
            assert token_data["id"] == the_dude.id

    async def test_refresh_jwt_token(
        self,
        app,
        user_class,
        default_guard,
        validating_user_class,
    ):
        """
        This test::
            * verifies that the refresh_jwt_token properly generates
              a refreshed jwt token.
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
        guard = Praetorian(app, user_class)
        the_dude = await User.create(username="TheDude",
                                     password=guard.hash_password("abides"),
                                     email='thedude@foo.com', roles="admin,operator")

        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await guard.encode_jwt_token(the_dude)
        new_moment = (
            pendulum.parse("2017-05-21 18:39:55")
            + DEFAULT_JWT_ACCESS_LIFESPAN
            + pendulum.Duration(minutes=1)
        )
        with plummet.frozen_time(new_moment):
            new_token = await guard.refresh_jwt_token(token)
            new_token_data = jwt.decode(
                new_token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert new_token_data["iat"] == new_moment.int_timestamp
            assert (
                new_token_data["exp"]
                == (new_moment + DEFAULT_JWT_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                new_token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_JWT_REFRESH_LIFESPAN).int_timestamp
            )
            assert new_token_data["id"] == the_dude.id
            assert new_token_data["rls"] == "admin,operator"

        moment = plummet.momentize("2017-05-21 18:39:55")
        with plummet.frozen_time('2017-05-21 18:39:55'):
            token = await guard.encode_jwt_token(the_dude)
        new_moment = (
            pendulum.parse("2017-05-21 18:39:55")
            + DEFAULT_JWT_ACCESS_LIFESPAN
            + pendulum.Duration(minutes=1)
        )
        with plummet.frozen_time(new_moment):
            new_token = await guard.refresh_jwt_token(
                token,
                override_access_lifespan=pendulum.Duration(hours=2),
            )
            new_token_data = jwt.decode(
                new_token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert (
                new_token_data["exp"]
                == (new_moment + pendulum.Duration(hours=2)).int_timestamp
            )

        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await guard.encode_jwt_token(
                the_dude,
                override_refresh_lifespan=pendulum.Duration(hours=2),
                override_access_lifespan=pendulum.Duration(minutes=30),
            )
        new_moment = moment + pendulum.Duration(minutes=31)
        with plummet.frozen_time(new_moment):
            new_token = await guard.refresh_jwt_token(
                token,
                override_access_lifespan=pendulum.Duration(hours=2),
            )
            new_token_data = jwt.decode(
                new_token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert (
                new_token_data["exp"]
                == new_token_data[REFRESH_EXPIRATION_CLAIM]
            )

        expiring_interval = DEFAULT_JWT_ACCESS_LIFESPAN + pendulum.Duration(
            minutes=1
        )
        validating_guard = Praetorian(app, validating_user_class)
        brandt = await validating_user_class.create(username="brandt", 
                                   password=guard.hash_password("can't watch"), 
                                   email='brandt@foo.com',
                                   is_active=True
                                  )
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await guard.encode_jwt_token(brandt)
        new_moment = moment + expiring_interval
        with plummet.frozen_time(new_moment):
            await validating_guard.refresh_jwt_token(token)
        brandt.is_active = False
        await brandt.save(update_fields=["is_active"])
        new_moment = new_moment + expiring_interval
        with plummet.frozen_time(new_moment):
            with pytest.raises(InvalidUserError) as err_info:
                await validating_guard.refresh_jwt_token(token)
        expected_message = "The user is not valid or has had access revoked"
        assert expected_message in str(err_info.value)

        expiring_interval = DEFAULT_JWT_ACCESS_LIFESPAN + pendulum.Duration(
            minutes=1
        )
        guard = Praetorian(app, user_class)
        bunny = await user_class.create(
            username="bunny",
            password=guard.hash_password("can't blow that far"),
            email="bunny@foo.com",
            roles=""
        )
        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            token = await guard.encode_jwt_token(bunny)
        await bunny.delete()
        new_moment = moment + expiring_interval
        with plummet.frozen_time(new_moment):
            with pytest.raises(MissingUserError) as err_info:
                await validating_guard.refresh_jwt_token(token)
        expected_message = "Could not find the requested user"
        assert expected_message in str(err_info.value)

        moment = plummet.momentize('2018-08-14 09:05:24')
        with plummet.frozen_time(moment):
            token = await guard.encode_jwt_token(
                the_dude,
                duder="brief",
                el_duderino="not brief",
            )
        new_moment = (
            pendulum.parse("2018-08-14 09:05:24")
            + DEFAULT_JWT_ACCESS_LIFESPAN
            + pendulum.Duration(minutes=1)
        )
        with plummet.frozen_time(new_moment):
            new_token = await guard.refresh_jwt_token(token)
            new_token_data = jwt.decode(
                new_token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert new_token_data["iat"] == new_moment.int_timestamp
            assert (
                new_token_data["exp"]
                == (new_moment + DEFAULT_JWT_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                new_token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_JWT_REFRESH_LIFESPAN).int_timestamp
            )
            assert new_token_data["id"] == the_dude.id
            assert new_token_data["rls"] == "admin,operator"
            assert new_token_data["duder"] == "brief"
            assert new_token_data["el_duderino"] == "not brief"

        await the_dude.delete()
        await brandt.delete()
        await bunny.delete()

    async def test_read_token_from_header(self, app, user_class):
        """
        This test verifies that a token may be properly read from a flask
        request's header using the configuration settings for header name and
        type
        """
        _client = ReusableClient(app, host='127.0.0.1', port='8000')
        with _client:
            guard = Praetorian(app, user_class)
            the_dude = await user_class.create(
                username="TheDude",
                password=guard.hash_password("abides"),
                email="thedude@foo.com",
                roles="admin,operator",
            )
    
            with plummet.frozen_time('2017-05-21 18:39:55'):
                token = await guard.encode_jwt_token(the_dude)

            request, _ = _client.get(
                "/unprotected",
                headers={
                    "Content-Type": "application/json",
                    DEFAULT_JWT_HEADER_NAME: DEFAULT_JWT_HEADER_TYPE + " " + token,
                },
            )
            logger.critical(f'Request Sent Headers: {request.headers}')

            assert guard.read_token_from_header(request) == token
            assert guard.read_token(request) == token
            await the_dude.delete()

    async def test_read_token_from_cookie(
        self, app, user_class
    ):
        """
        This test verifies that a token may be properly read from a flask
        request's cookies using the configuration settings for cookie
        """
        _client = ReusableClient(app, host='127.0.0.1', port='8000')
        with _client:
            guard = Praetorian(app, user_class)
            the_dude = await user_class.create(
                username="TheDude",
                email="thedude@foo.com",
                password=guard.hash_password("abides"),
                roles="admin,operator",
            )

            cookies = Cookies()
    
            with plummet.frozen_time('2017-05-21 18:39:55'):
                token = await guard.encode_jwt_token(the_dude)
                cookies[guard.cookie_name] = token
                #with use_cookie(token):
                request, _ = _client.get(
                    "/unprotected",
                    cookies=cookies
                )
   
            assert guard.read_token_from_cookie(request) == token
            assert guard.read_token(request) == token
  
            await the_dude.delete()

    async def test_pack_header_for_user(self, app, user_class):
        """
        This test::
          * verifies that the pack_header_for_user method can be used to
            package a token into a header dict for a specified user
          * verifies that custom claims may be packaged as well
        """
        guard = Praetorian(app, user_class)
        the_dude = user_class(
            username="TheDude",
            password=guard.hash_password("abides"),
            roles="admin,operator",
        )

        moment = plummet.momentize('2017-05-21 18:39:55')
        with plummet.frozen_time(moment):
            header_dict = await guard.pack_header_for_user(the_dude)
            token_header = header_dict.get(DEFAULT_JWT_HEADER_NAME)
            assert token_header is not None
            token = token_header.replace(DEFAULT_JWT_HEADER_TYPE, "")
            token = token.strip()
            token_data = jwt.decode(
                token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + DEFAULT_JWT_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_JWT_REFRESH_LIFESPAN).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"

        moment = plummet.momentize('2017-05-21 18:39:55')
        override_access_lifespan = pendulum.Duration(minutes=1)
        override_refresh_lifespan = pendulum.Duration(hours=1)
        with plummet.frozen_time(moment):
            header_dict = await guard.pack_header_for_user(
                the_dude,
                override_access_lifespan=override_access_lifespan,
                override_refresh_lifespan=override_refresh_lifespan,
            )
            token_header = header_dict.get(DEFAULT_JWT_HEADER_NAME)
            assert token_header is not None
            token = token_header.replace(DEFAULT_JWT_HEADER_TYPE, "")
            token = token.strip()
            token_data = jwt.decode(
                token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
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
            header_dict = await guard.pack_header_for_user(
                the_dude,
                duder="brief",
                el_duderino="not brief",
            )
            token_header = header_dict.get(DEFAULT_JWT_HEADER_NAME)
            assert token_header is not None
            token = token_header.replace(DEFAULT_JWT_HEADER_TYPE, "")
            token = token.strip()
            token_data = jwt.decode(
                token,
                guard.encode_key,
                algorithms=guard.allowed_algorithms,
            )
            assert token_data["iat"] == moment.int_timestamp
            assert (
                token_data["exp"]
                == (moment + DEFAULT_JWT_ACCESS_LIFESPAN).int_timestamp
            )
            assert (
                token_data[REFRESH_EXPIRATION_CLAIM]
                == (moment + DEFAULT_JWT_REFRESH_LIFESPAN).int_timestamp
            )
            assert token_data["id"] == the_dude.id
            assert token_data["rls"] == "admin,operator"
            assert token_data["duder"] == "brief"
            assert token_data["el_duderino"] == "not brief"

    async def test_reset_email(self, app, user_class, tmpdir, default_guard):
        """
        This test verifies email based password reset functions as expected.
        This includes sending messages with valid time expiring JWT tokens
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
        app.config["PRAETORIAN_EMAIL_TEMPLATE"] = str(template_file)
        app.config["PRAETORIAN_RESET_ENDPOINT"] = "unprotected"

        # create our default test user
        the_dude = await user_class.create(username="TheDude",
                                           email="thedude@foo.com",
                                           password="blah")

        with app.ctx.mail.record_messages() as outbox:
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
            assert len(outbox) == 1

            assert not notify["result"]

        # test our token is good
        jwt_data = await default_guard.extract_jwt_token(
            notify["token"],
            access_type=AccessType.reset,
        )
        assert jwt_data[IS_RESET_TOKEN_CLAIM]

        validated_user = await default_guard.validate_reset_token(token)
        logger.critical(f'Validated User: {validated_user}')
        logger.critical(f'The Dude: {the_dude}')
        assert validated_user == the_dude

        await the_dude.delete()

    async def test_registration_email(
        self,
        app,
        user_class,
        tmpdir,
        default_guard,
    ):
        """
        This test verifies email based registration functions as expected.
        This includes sending messages with valid time expiring JWT tokens
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
        app.config["PRAETORIAN_EMAIL_TEMPLATE"] = str(template_file)
        app.config["PRAETORIAN_CONFIRMATION_ENDPOINT"] = "unprotected"

        # create our default test user
        the_dude = await user_class.create(username="TheDude",
                                           email="thedude@foo.com",
                                           password=default_guard.hash_password("Abides"))

        with app.ctx.mail.record_messages() as outbox:
            notify = await default_guard.send_registration_email(
                "the@dude.com",
                user=the_dude,
                confirmation_sender="you@whatever.com",
            )
            token = notify["token"]

            # test our own interpretation and what we got back from flask_mail
            assert token in notify["message"]
            assert len(outbox) == 1

            assert not notify["result"]

        # test our token is good
        jwt_data = await default_guard.extract_jwt_token(
            notify["token"],
            access_type=AccessType.register,
        )
        assert jwt_data[IS_REGISTRATION_TOKEN_CLAIM]

    async def test_get_user_from_registration_token(
        self,
        app,
        user_class,
        default_guard,
    ):
        """
        This test verifies that a user can be extracted from an email based
        registration token. Also verifies that a token that has expired
        cannot be used to fetch a user. Also verifies that a registration
        token may not be refreshed
        """
        # create our default test user
        the_dude = await user_class.create(
            username="TheDude",
            email="the@dude.com",
            password=default_guard.hash_password("abides"),
        )

        reg_token = await default_guard.encode_jwt_token(
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
            expired_reg_token = await default_guard.encode_jwt_token(
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

    async def test_validate_and_update(self, app, user_class, default_guard):
        """
        This test verifies that Praetorian hashes passwords using the scheme
        specified by the HASH_SCHEME setting. If no scheme is supplied, the
        test verifies that the default scheme is used. Otherwise, the test
        verifies that the hashed password matches the supplied scheme.
        """
        pbkdf2_sha512_password = default_guard.hash_password("pbkdf2_sha512")

        # create our default test user
        the_dude = await user_class.create(
            username="TheDude",
            email="the@dude.com",
            password=pbkdf2_sha512_password,
        )

        """
        Test the current password is hashed with PRAETORIAN_HASH_SCHEME
        """
        assert await default_guard.verify_and_update(user=the_dude)

        """
        Test a password hashed with something other than
            PRAETORIAN_HASH_ALLOWED_SCHEME triggers an Exception.
        """
        app.config["PRAETORIAN_HASH_SCHEME"] = "bcrypt"
        default_guard = Praetorian(app, user_class)
        bcrypt_password = default_guard.hash_password("bcrypt_password")
        the_dude.password = bcrypt_password

        del app.config["PRAETORIAN_HASH_SCHEME"]
        app.config["PRAETORIAN_HASH_DEPRECATED_SCHEMES"] = ["bcrypt"]
        default_guard = Praetorian(app, user_class)
        with pytest.raises(LegacyScheme):
            await default_guard.verify_and_update(the_dude)

        """
        Test a password hashed with something other than
            PRAETORIAN_HASH_SCHEME, and supplied good password
            gets the user entry's password updated and saved.
        """
        the_dude_old_password = the_dude.password
        updated_dude = await default_guard.verify_and_update(
            the_dude, "bcrypt_password"
        )
        assert updated_dude.password != the_dude_old_password

        """
        Test a password hashed with something other than
            PRAETORIAN_HASH_SCHEME, and supplied bad password
            gets an Exception raised.
        """
        the_dude.password = bcrypt_password
        with pytest.raises(AuthenticationError):
            await default_guard.verify_and_update(user=the_dude, password="failme")

        # put away your toys
        await the_dude.delete()

    async def test_authenticate_validate_and_update(self, app, user_class):
        """
        This test verifies the authenticate() function, when altered by
        either 'PRAETORIAN_HASH_AUTOUPDATE' or 'PRAETORIAN_HASH_AUTOTEST'
        performs the authentication and the required subaction.
        """

        default_guard = Praetorian(app, user_class)
        pbkdf2_sha512_password = default_guard.hash_password("start_password")

        # create our default test user
        the_dude = await user_class.create(
            username="TheDude",
            email="the@dude.com",
            password=pbkdf2_sha512_password,
        )

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
        app.config["PRAETORIAN_HASH_SCHEME"] = "bcrypt"
        default_guard = Praetorian(app, user_class)
        bcrypt_password = default_guard.hash_password("bcrypt_password")
        the_dude.password = bcrypt_password
        await the_dude.save(update_fields=["password"])

        del app.config["PRAETORIAN_HASH_SCHEME"]
        app.config["PRAETORIAN_HASH_DEPRECATED_SCHEMES"] = ["bcrypt"]
        app.config["PRAETORIAN_HASH_AUTOTEST"] = True
        default_guard = Praetorian(app, user_class)
        with pytest.raises(LegacyScheme):
            await default_guard.authenticate(the_dude.username, "bcrypt_password")

        """
        Test the updated model with a bad hash scheme and AUTOUPDATE enabled.
        Should return an updated user object we need to save ourselves.
        """
        the_dude_old_password = the_dude.password
        app.config["PRAETORIAN_HASH_AUTOUPDATE"] = True
        default_guard = Praetorian(app, user_class)
        updated_dude = await default_guard.authenticate(
            the_dude.username, "bcrypt_password"
        )
        assert updated_dude.password != the_dude_old_password

        # put away your toys
        await the_dude.delete()
