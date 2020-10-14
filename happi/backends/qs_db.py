"""
Backend implementation for parsing the Questionnaire
"""
import functools
import logging
import re
from typing import Optional

from psdm_qs_cli import QuestionnaireClient

from ..errors import DatabaseError
from .json_db import JSONBackend

logger = logging.getLogger(__name__)


class RequiredKeyError(KeyError):
    """Required key not found in questionnaire."""
    ...


class QuestionnaireHelper:
    device_translations = {
        'motors': 'Motor',
        'trig': 'Trigger',
        'ao': 'Acromag',
        'ai': 'Acromag'
    }

    def __init__(self, client: QuestionnaireClient):
        self._client = client
        self._experiment = None
        self.experiment_to_proposal = client.getExpName2URAWIProposalIDs()

    @property
    def experiment(self) -> str:
        """The experiment name """
        return self._experiment

    @experiment.setter
    def experiment(self, experiment: str):
        self._experiment = experiment

        # Proposals are per-experiment: clear the cache.
        self.get_proposal_list.cache_clear()
        self.get_run_details.cache_clear()

    @property
    def proposal(self):
        """Get the proposal number for the configured experiment."""
        if self.experiment is None:
            raise RuntimeError('Experiment unset')

        try:
            return self.experiment_to_proposal[self.experiment]
        except KeyError:
            # Rare case for debug/daq experiments, roll with it for now
            return self.experiment

    @property
    def run_number(self):
        """Get the run number from the experiment."""
        if self.experiment is None or len(self.experiment) <= 2:
            raise RuntimeError(f'Experiment invalid: {self.experiment}')

        run_number = self.experiment[-2:]
        return f'run{run_number}'

    @functools.lru_cache()
    def get_proposal_list(self, run_number: str) -> dict:
        """
        Get the proposal list for a given run number.

        Parameters
        ----------
        run_number : str
            The run number.

        Raises
        ------
        DatabaseError
        """
        try:
            logger.debug("Requesting list of proposals in %s", run_number)
            return self._client.getProposalsListForRun(run_number)
        except KeyError as ex:
            # Invalid proposal id for this run
            raise DatabaseError(
                f'Unable to find proposal {self.proposal}'
            ) from ex
        except Exception as ex:
            # Find if our exception gave an HTTP status code and interpret it
            status_code = ex.args[1] if len(ex.args) >= 2 else ''
            if status_code == 500:
                # No information found from run
                reason = f'No run id found for {run_number}'
            elif status_code == 401:
                # Invalid credentials
                reason = 'Invalid credentials'
            else:
                # Unrecognized error
                reason = 'Unable to find run information'
            raise DatabaseError(reason) from ex

    def get_beamline_from_run(self, run_number: str) -> str:
        """
        Determine the beamline from a proposal + run_number.

        Parameters
        ----------
        run_number : str
            The run number.

        Returns
        -------
        beamline : str
        """
        return self.get_proposal_list(run_number)[self.proposal]['Instrument']

    @functools.lru_cache()
    def get_run_details(self, run_number: str) -> dict:
        """
        Get details of the run in a raw dictionary.
        """
        return self._client.getProposalDetailsForRun(
            run_number, self.proposal
        )

    @staticmethod
    def translate_devices(run_details: dict, table_name: str, class_name: str):
        pattern = re.compile(rf'pcdssetup-{table_name}-(\d+)-(\w+)')

        devices = {}
        for field, value in run_details.items():
            match = pattern.match(field)
            if match:
                device_number, name = match.groups()

                if device_number not in devices:
                    devices[device_number] = {}

                # Add the key information to the specific device dictionary
                devices[device_number][name] = value

        return devices

    @staticmethod
    def create_db_item(info: dict,
                       beamline: str,
                       class_name: str
                       ) -> dict:
        """
        Create one database entry given translated questionnaire information.

        Parameters
        ----------
        """
        # Shallow-copy to not modify the original:
        info = dict(info)

        name = info.pop('name')

        # Create our happi JSON-backend equivalent:
        entry = {
            '_id': name,
            'name': name,
            'prefix': info['pvbase'],
            'beamline': beamline,
            'type': class_name,
            **info,
        }

        # Empty strings from the Questionnaire make for invalid entries:
        for key in {'prefix', 'name'}:
            if not entry.get(key):
                raise RequiredKeyError(
                    f"Unable to create a device without key {key}"
                )

        return entry

    @staticmethod
    def to_database(beamline: str,
                    run_details: dict,
                    *,
                    device_translations: Optional[dict] = None
                    ) -> dict:
        """
        Translate a set of run details into a happi-compatible dictionary.
        """

        happi_db = {}
        if device_translations is None:
            device_translations = QuestionnaireHelper.device_translations

        for table_name, class_name in device_translations.items():
            devices = QuestionnaireHelper.translate_devices(
                run_details, table_name, class_name)

            if not devices:
                logger.info(
                    "No device information found under '%s'", table_name
                )
                continue

            for device_number, device_info in devices.items():
                logger.debug(
                    '[%s:%s] Found %s', table_name, device_number, device_info
                )
                try:
                    entry = QuestionnaireHelper.create_db_item(
                        device_number
                    )
                except RequiredKeyError:
                    logger.debug(
                        'Missing key for %s:%s', table_name, device_number,
                        exc_info=True
                    )
                except Exception as ex:
                    logger.warning(
                        'Failed to create a happi database entry from the '
                        'questionnaire device: %s:%s. %s: %s',
                        table_name, device_number, ex.__class__.__name__, ex,
                    )
                else:
                    identifier = entry['_id']
                    if identifier in happi_db:
                        logger.warning(
                            'Questionnaire name clash: %s (was: %s now: %s)',
                            identifier, happi_db[identifier], entry
                        )
                    happi_db[identifier] = entry

        return happi_db


class QSBackend(JSONBackend):
    """
    Questionniare Backend

    This backend connects to the LCLS questionnaire and looks at devices with
    the key pattern pcds-{}-setup-{}-{}. These fields are then combined and
    turned into proper happi devices. The translation of table name to
    ``happi.HappiItem`` is determined by the :attr:`.device_translations`
    dictionary. The beamline is determined by looking where the proposal was
    submitted.

    Unlike the other backends, this one is read-only. All changes to the device
    information should be done via the web interface. Finally, in order to
    avoid duplicating any code needed to search the device database, the
    QSBackend inherits directly from JSONBackend. Many of the functions are
    unmodified with exception being that this backend merely searchs through an
    in memory dictionary while the JSONBackend reads from the file before
    searches.

    Parameters
    ----------
    expname : str
        The experiment name from the elog, e.g. xcslp1915

    url : str, optional
        Provide a base URL for the Questionnaire. If left as None the
        appropriate URL will be chosen based on your authentication method

    use_kerberos : bool, optional
        Use a Kerberos ticket to login to the Questionnaire. This is the
        default authentication method

    user : str, optional
        A username for ws_auth sign-in. If not provided the current login name
        is used

    pw : str, optional
        A password for ws_auth sign-in. If not provided a password will be
        requested
    """
    device_translations = {'motors': 'Motor', 'trig': 'Trigger',
                           'ao': 'Acromag', 'ai': 'Acromag'}

    def __init__(self, expname, *, url=None, use_kerberos=True, user=None,
                 pw=None):
        # Create our client and gather the raw information from the client
        self._client = QuestionnaireClient(
            url=url, use_kerberos=use_kerberos, user=user, pw=pw
        )

        self.db = self._initialize_database(expname)

    def _initialize_database(self, experiment):
        """Initialize and convert the questionnaire."""
        try:
            self.experiment = experiment
            self.helper = QuestionnaireHelper(self._client)

            self.helper.experiment = experiment
            run_number = self.helper.run_number
            beamline = self.helper.get_beamline_from_run(run_number)
            run_details = self.helper.get_run_details(run_number)
            return self.helper.to_database(
                beamline=beamline,
                run_details=run_details,
                device_translations=self.device_translations
            )
        except Exception:
            logger.error('Failed to load the questionnaire', exc_info=True)
            return {}

    def initialize(self):
        """
        Can not initialize a new Questionnaire entry from API
        """
        raise NotImplementedError("The Questionnaire backend is read-only")

    def load(self):
        """
        Return the structured dictionary of information
        """
        return self.db

    def store(self, *args, **kwargs):
        """
        The current implementation of this backend is read-only
        """
        raise NotImplementedError("The Questionnaire backend is read-only")

    def save(self, *args, **kwargs):
        """
        The current implementation of this backend is read-only
        """
        raise NotImplementedError("The Questionnaire backend is read-only")

    def delete(self, _id):
        """
        The current implementation of this backend is read-only
        """
        raise NotImplementedError("The Questionnaire backend is read-only")
