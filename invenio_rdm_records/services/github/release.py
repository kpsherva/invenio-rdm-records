# -*- coding: utf-8 -*-
#
# Copyright (C) 2023 CERN.
#
# Invenio-RDM-Records is free software; you can redistribute it and/or modify
# it under the terms of the MIT License; see LICENSE file for more details.
"""Github release API implementation."""

from flask import current_app
from invenio_access.permissions import system_identity
from invenio_db import db
from invenio_github.api import GitHubRelease
from invenio_github.models import ReleaseStatus
from invenio_records_resources.services.uow import UnitOfWork

from ..errors import RecordDeletedException
from ...proxies import current_rdm_records_service
from ...resources.serializers.ui import UIJSONSerializer
from .metadata import RDMReleaseMetadata
from .utils import retrieve_recid_by_uuid


class RDMGithubRelease(GitHubRelease):
    """Implement release API instance for RDM."""

    @property
    def metadata(self):
        """Extracts metadata to create an RDM draft."""
        metadata = RDMReleaseMetadata(self)
        output = metadata.default_metadata
        output.update(metadata.citation_metadata)
        return output

    def resolve_record(self):
        """Resolves an RDM record from a release."""
        if not self.release_object.record_id:
            return None
        recid = retrieve_recid_by_uuid(self.release_object.record_id)
        try:
            return current_rdm_records_service.read(system_identity, recid.pid_value)
        except RecordDeletedException:
            return None

    def _upload_files_to_draft(self, draft, draft_file_service, uow):
        """Upload files to draft."""
        # Validate the release files are fetchable
        self.test_zipball()

        draft_file_service.init_files(
            self.user_identity,
            draft.id,
            data=[{"key": self.release_file_name}],
            uow=uow,
        )

        with self.fetch_zipball_file() as file_stream:
            draft_file_service.set_file_content(
                self.user_identity,
                draft.id,
                self.release_file_name,
                file_stream,
                uow=uow,
            )

    def publish(self):
        """Publish GitHub release as record.

        Drafts and records are created using the current records service.
        The following steps are run inside a single transaction:

        - Create a draft.
        - The draft's ownership is set to the user's id via its parent.
        - Upload files to the draft.
        - Publish the draft.

        In case of failure, the transaction is rolled back and the release status set to 'FAILED'

        :raises ex: any exception generated by the records service (e.g. invalid metadata)
        """
        try:
            self.release_processing()
            # Commit state change, in case the publishing is stuck
            db.session.commit()

            draft_file_service = current_rdm_records_service.draft_files

            with UnitOfWork(db.session) as uow:
                data = {
                    "metadata": self.metadata,
                    "access": {"record": "public", "files": "public"},
                    "files": {"enabled": True},
                }

                if self.is_first_release():
                    draft = current_rdm_records_service.create(
                        self.user_identity, data, uow=uow
                    )
                    self._upload_files_to_draft(draft, draft_file_service, uow)
                else:
                    # Retrieve latest record id and its recid
                    latest_record_uuid = self.repository_object.latest_release(
                        ReleaseStatus.PUBLISHED
                    ).record_id

                    recid = retrieve_recid_by_uuid(latest_record_uuid)

                    # Create a new version and update its contents
                    new_version_draft = current_rdm_records_service.new_version(
                        self.user_identity, recid.pid_value, uow=uow
                    )

                    self._upload_files_to_draft(
                        new_version_draft, draft_file_service, uow
                    )

                    draft = current_rdm_records_service.update_draft(
                        self.user_identity, new_version_draft.id, data, uow=uow
                    )

                draft_file_service.commit_file(
                    self.user_identity, draft.id, self.release_file_name, uow=uow
                )

                record = current_rdm_records_service.publish(
                    self.user_identity, draft.id, uow=uow
                )

                # Update release weak reference and set status to PUBLISHED
                self.release_object.record_id = record._record.model.id
                self.release_published()

                # UOW must be committed manually since we're not using the decorator
                uow.commit()
                return record
        except Exception as ex:
            # Flag release as FAILED and raise the exception
            self.release_failed()
            # Commit the FAILED state, other changes were already rollbacked by the UOW
            db.session.commit()
            raise ex

    def process_release(self):
        """Processes a github release.

        The release might be first validated, in terms of sender, and then published.

        :raises ex: any exception generated by the records service when creating a draft or publishing the release record.
        """
        try:
            record = self.publish()
            return record
        except Exception as ex:
            current_app.logger.exception(
                f"Error while processing GitHub release {self.release_object.id}: {str(ex)}"
            )
            raise ex

    def serialize_record(self):
        """Serializes an RDM record."""
        return UIJSONSerializer().serialize_object(self.record.data)

    @property
    def record_url(self):
        """Release self url points to RDM record.

        It points to DataCite URL if the integration is enabled, otherwise it points to the HTML URL.
        """
        html_url = self.record.data["links"]["self_html"]
        doi_url = self.record.data["links"].get("doi")
        return doi_url or html_url

    @property
    def badge_title(self):
        """Returns the badge title."""
        if current_app.config.get("DATACITE_ENABLED"):
            return "DOI"

    @property
    def badge_value(self):
        """Returns the badge value."""
        if current_app.config.get("DATACITE_ENABLED"):
            return self.record.data.get("pids", {}).get("doi", {}).get("identifier")
