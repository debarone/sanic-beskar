from tortoise.exceptions import DoesNotExist


class TortoiseUserMixin:
    """
    A short-cut providing required methods and attributes for a user class
    implemented with tortoise-orm. Makes many assumptions about how the class
    is defined.

    ASSUMPTIONS:
    * The model has an ``id`` column that uniquely identifies each instance
    * The model has a ``rolenames`` column that contains the roles for the
    user instance as a comma separated list of roles
    * The model has a ``username`` column that is a unique string for each instance
    * The model has a ``password`` column that contains its hashed password

    """

    @property
    def identity(self):
        """
        Provides the required attribute or property ``identity``
        """
        return self.id

    @property
    def rolenames(self):
        """
        Provides the required attribute or property ``rolenames``
        """
        try:
            return self.roles.split(",")
        except Exception:
            return []

    @classmethod
    async def lookup(cls, username=None, email=None):
        try:
            if username:
                return await cls.filter(username=username).get()
            elif email:
                return await cls.filter(email=email).get()
            else:
                return None
        except DoesNotExist:
            return None

    @classmethod
    async def identify(cls, id):
        try:
            return await cls.filter(id=id).get()
        except DoesNotExist:
            return None
