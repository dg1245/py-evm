import collections
from typing import cast, Dict, Set, Union  # noqa: F401
import uuid

from eth_utils.toolz import (
    first,
    merge,
    last,
)
from eth_utils import (
    ValidationError,
)

from eth.db.backends.base import BaseDB
from eth.db.diff import DBDiff, DBDiffTracker


class DeletedEntry:
    pass


DELETED_ENTRY = DeletedEntry()


class Journal(BaseDB):
    """
    A Journal is an ordered list of changesets.  A changeset is a dictionary
    of database keys and values.  The values are tracked changes that were
    written after the changeset was created

    Changesets are referenced by a random uuid4.
    """

    def __init__(self) -> None:
        # contains a mapping from all of the `uuid4` changeset_ids
        # to a dictionary of key:value pairs with the recorded changes
        # that belong to the changeset
        self.journal_data = collections.OrderedDict()  # type: collections.OrderedDict[uuid.UUID, Dict[bytes, Union[bytes, DeletedEntry]]]  # noqa E501
        self._clears_at = set()  # type: Set[uuid.UUID]

    @property
    def root_changeset_id(self) -> uuid.UUID:
        """
        Returns the id of the root changeset
        """
        return first(self.journal_data.keys())

    @property
    def latest_id(self) -> uuid.UUID:
        """
        Returns the id of the latest changeset
        """
        return last(self.journal_data.keys())

    @property
    def latest(self) -> Dict[bytes, Union[bytes, DeletedEntry]]:
        """
        Returns the dictionary of db keys and values for the latest changeset.
        """
        return self.journal_data[self.latest_id]

    @latest.setter
    def latest(self, value: Dict[bytes, Union[bytes, DeletedEntry]]) -> None:
        """
        Setter for updating the *latest* changeset.
        """
        self.journal_data[self.latest_id] = value

    def is_empty(self) -> bool:
        return len(self.journal_data) == 0

    def has_changeset(self, changeset_id: uuid.UUID) -> bool:
        return changeset_id in self.journal_data

    def record_changeset(self) -> uuid.UUID:
        """
        Creates a new changeset. Changesets are referenced by a random uuid4
        to prevent collisions between multiple changesets.
        """
        changeset_id = uuid.uuid4()
        self.journal_data[changeset_id] = {}
        return changeset_id

    def pop_changeset(self, changeset_id: uuid.UUID) -> Dict[bytes, Union[bytes, DeletedEntry]]:
        """
        Returns all changes from the given changeset.  This includes all of
        the changes from any subsequent changeset, giving precidence to
        later changesets.
        """
        if changeset_id not in self.journal_data:
            raise KeyError(changeset_id, "Unknown changeset in JournalDB")

        all_ids = tuple(self.journal_data.keys())
        changeset_idx = all_ids.index(changeset_id)
        changesets_to_pop = all_ids[changeset_idx:]
        popped_clears = tuple(idx for idx in changesets_to_pop if idx in self._clears_at)
        if popped_clears:
            last_clear_idx = changesets_to_pop.index(popped_clears[-1])
            changesets_to_drop = changesets_to_pop[:last_clear_idx]
            changesets_to_merge = changesets_to_pop[last_clear_idx:]
        else:
            changesets_to_drop = ()
            changesets_to_merge = changesets_to_pop

        # we pull all of the changesets *after* the changeset we are
        # reverting to and collapse them to a single set of keys (giving
        # precedence to later changesets)
        changeset_data = merge(*(
            self.journal_data.pop(c_id)
            for c_id
            in changesets_to_merge
        ))

        # drop the changes on the floor if they came before a clear that is being committed
        for changeset_id in changesets_to_drop:
            self.journal_data.pop(changeset_id)

        self._clears_at.difference_update(popped_clears)
        return changeset_data

    def clear(self) -> None:
        """
        Treat as if the *underlying* database will also be cleared by some other mechanism.
        We build a special empty changeset just for marking that all previous data should
        be ignored.
        """
        # these internal records are used as a way to tell the difference between
        # changes that came before and after the clear
        self.record_changeset()
        self._clears_at.add(self.latest_id)
        self.record_changeset()

    def has_clear(self, check_changeset_id: uuid.UUID) -> bool:
        for changeset_id in reversed(self.journal_data.keys()):
            if changeset_id in self._clears_at:
                return True
            elif check_changeset_id == changeset_id:
                return False
        raise ValidationError("Changeset ID %s is not in the journal" % check_changeset_id)

    def commit_changeset(self, changeset_id: uuid.UUID) -> Dict[bytes, Union[bytes, DeletedEntry]]:
        """
        Collapses all changes for the given changeset into the previous
        changesets if it exists.
        """
        does_clear = self.has_clear(changeset_id)
        changeset_data = self.pop_changeset(changeset_id)
        if not self.is_empty():
            # we only have to assign changeset data into the latest changeset if
            # there is one.
            if does_clear:
                # if there was a clear and more changesets underneath then clear the latest
                # changeset, and replace with a new clear changeset
                self.latest = {}
                self._clears_at.add(self.latest_id)
                self.record_changeset()
                self.latest = changeset_data
            else:
                # otherwise, merge in all the current data
                self.latest = merge(
                    self.latest,
                    changeset_data,
                )
        return changeset_data

    #
    # Database API
    #
    def __getitem__(self, key: bytes) -> Union[bytes, DeletedEntry]:    # type: ignore # Breaks LSP
        """
        For key lookups we need to iterate through the changesets in reverse
        order, returning from the first one in which the key is present.
        """
        # Ignored from mypy because of https://github.com/python/typeshed/issues/2078
        for changeset_id, changeset_data in reversed(self.journal_data.items()):
            if changeset_id in self._clears_at:
                return DELETED_ENTRY
            elif key in changeset_data:
                return changeset_data[key]
            else:
                continue

        return None

    def __setitem__(self, key: bytes, value: bytes) -> None:
        self.latest[key] = value

    def _exists(self, key: bytes) -> bool:
        val = self.get(key)
        return val is not None and val is not DELETED_ENTRY

    def __delitem__(self, key: bytes) -> None:
        self.latest[key] = DELETED_ENTRY


class JournalDB(BaseDB):
    """
    A wrapper around the basic DB objects that keeps a journal of all changes.
    Each time a recording is started, the underlying journal creates a new
    changeset and assigns an id to it. The journal then keeps track of all changes
    that go into this changeset.

    Discarding a changeset simply throws it away inculding all subsequent changesets
    that may have followed. Commiting a changeset merges the given changeset and all
    subsequent changesets into the previous changeset giving precidence to later
    changesets in case of conflicting keys.

    Nothing is written to the underlying db until `persist()` is called.

    The added memory footprint for a JournalDB is one key/value stored per
    database key which is changed.  Subsequent changes to the same key within
    the same changeset will not increase the journal size since we only need
    to track latest value for any given key within any given changeset.
    """
    wrapped_db = None
    journal = None  # type: Journal

    def __init__(self, wrapped_db: BaseDB) -> None:
        self.wrapped_db = wrapped_db
        self.reset()

    def __getitem__(self, key: bytes) -> bytes:

        val = self.journal[key]
        if val is DELETED_ENTRY:
            raise KeyError(key, "item was explicitly deleted in JournalDB")
        elif val is None:
            return self.wrapped_db[key]
        else:
            # mypy doesn't allow custom type guards yet so we need to cast here
            # even though we know it can only be `bytes` at this point.
            return cast(bytes, val)

    def __setitem__(self, key: bytes, value: bytes) -> None:
        """
        - replacing an existing value
        - setting a value that does not exist
        """
        self.journal[key] = value

    def _exists(self, key: bytes) -> bool:
        val = self.journal[key]
        if val is DELETED_ENTRY:
            return False
        elif val is None:
            return key in self.wrapped_db
        else:
            return True

    def clear(self) -> None:
        """
        Remove all keys. Immediately after a clear, *all* getitem requests will return a KeyError.
        That includes the changes pending persist and any data in the underlying database.

        (This action is journaled, like all other actions)

        clear will *not* persist the emptying of all keys in the underlying DB.
        It only prevents any updates (or deletes!) before it from being persisted.

        Any caller that wants to use clear must also make sure that the underlying database
        reflects their desired end state (maybe emptied, maybe not).
        """
        self.journal.clear()

    def has_clear(self) -> bool:
        return self.journal.has_clear(self.journal.root_changeset_id)

    def __delitem__(self, key: bytes) -> None:
        if key not in self.journal and key not in self.wrapped_db:
            raise KeyError(key, "key could not be deleted in JournalDB, because it was missing")
        del self.journal[key]

    #
    # Snapshot API
    #
    def _validate_changeset(self, changeset_id: uuid.UUID) -> None:
        """
        Checks to be sure the changeset is known by the journal
        """
        if not self.journal.has_changeset(changeset_id):
            raise ValidationError("Changeset not found in journal: {0}".format(
                str(changeset_id)
            ))

    def record(self) -> uuid.UUID:
        """
        Starts a new recording and returns an id for the associated changeset
        """
        return self.journal.record_changeset()

    def discard(self, changeset_id: uuid.UUID) -> None:
        """
        Throws away all journaled data starting at the given changeset
        """
        self._validate_changeset(changeset_id)
        self.journal.pop_changeset(changeset_id)

    def commit(self, changeset_id: uuid.UUID) -> None:
        """
        Commits a given changeset. This merges the given changeset and all
        subsequent changesets into the previous changeset giving precidence
        to later changesets in case of any conflicting keys.

        If this is the base changeset then all changes will be written to
        the underlying database and the Journal starts a new recording.
        """
        self._validate_changeset(changeset_id)
        journal_data = self.journal.commit_changeset(changeset_id)

        if self.journal.is_empty():
            for key, value in journal_data.items():
                if value is not DELETED_ENTRY:
                    self.wrapped_db[key] = value  # type: ignore # value can only be bytes here
                else:
                    try:
                        del self.wrapped_db[key]
                    except KeyError:
                        pass

            # Ensure the journal automatically restarts recording after
            # it has been persisted to the underlying db
            self.reset()

    def persist(self) -> None:
        """
        Persist all changes in underlying db
        """
        self.commit(self.journal.root_changeset_id)

    def reset(self) -> None:
        """
        Reset the entire journal.
        """
        self.journal = Journal()
        self.record()

    def diff(self) -> DBDiff:
        """
        Generate a DBDiff of all pending changes.
        These are the changes that would occur if :meth:`persist()` were called.
        """
        tracker = DBDiffTracker()
        visited_keys = set()  # type: Set[bytes]

        # Iterate in reverse, so you can skip over any keys from old checkpoints.
        # This is purely for performance, not correctness.
        for changeset in reversed(self.journal.journal_data.values()):
            for key, value in changeset.items():
                if key in visited_keys:
                    # this old change has already been tracked
                    continue
                elif value is DELETED_ENTRY:
                    del tracker[key]
                else:
                    tracker[key] = value  # type: ignore # This is always bytes, but mypy can't tell

                visited_keys.add(key)

        return tracker.diff()