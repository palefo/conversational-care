"""What an agent card says it is for.

The Agents page used to identify a native agent by its key alone: a card
reading ``link_worker`` told an admin nothing about what the agent did, and the
only way to find out was to read the code. Agents carry a description now, the
natives ship with one, and the card leads with it.

The fallback matters as much as the field. Every agent that predates this has a
blank description, and those cards must go on saying what they always said.

    python3 manage.py test ConvAI.test_agent_descriptions --settings=test_settings
"""

import importlib

from django.apps import apps as real_apps
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from ConvAI.models import Agent, ConvAIUser

seed_mod = importlib.import_module("ConvAI.migrations.0082_agent_description")

LINK_WORKER = seed_mod.NATIVE_DESCRIPTIONS["link_worker"]


class SeedingTheNatives(TestCase):
    """The migration writes the descriptions the natives ship with."""

    def setUp(self):
        self.agent = Agent.objects.create(
            name="Link Worker", kind=Agent.Kind.NATIVE, native_key="link_worker")

    def test_a_native_with_no_description_is_given_the_one_it_ships_with(self):
        seed_mod.seed_descriptions(real_apps, None)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.description, LINK_WORKER)

    def test_an_admin_s_own_wording_is_left_alone(self):
        # Re-running the migration against a live database must not talk over
        # whoever has since described the agent in their own terms.
        self.agent.description = "Books the visits, nothing else."
        self.agent.save(update_fields=["description"])
        seed_mod.seed_descriptions(real_apps, None)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.description, "Books the visits, nothing else.")

    def test_reversing_takes_back_only_what_it_wrote(self):
        seed_mod.seed_descriptions(real_apps, None)
        other = Agent.objects.create(name="Loopback", kind=Agent.Kind.NATIVE,
                                     native_key="loopback",
                                     description="Mine, not the platform's.")
        seed_mod.unseed_descriptions(real_apps, None)
        self.agent.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.agent.description, "")
        self.assertEqual(other.description, "Mine, not the platform's.")


class WhatTheCardCanHold(TestCase):
    """The shipped descriptions have to fit the space they are written for."""

    def test_every_native_description_fits_on_a_card(self):
        # The card gives a description two lines and trims the rest. A sentence
        # cut off mid-word is what this field was added to avoid, so the text
        # that ships with the platform must not need trimming at all.
        too_long = {
            key: len(text)
            for key, text in seed_mod.NATIVE_DESCRIPTIONS.items()
            if len(text) > seed_mod.CARD_BUDGET
        }
        self.assertEqual(too_long, {})


class TheAgentsPage(TestCase):
    """What each card shows, with a description and without one."""

    def setUp(self):
        self.admin = ConvAIUser.objects.create_superuser(
            username="admin", password="x")
        self.client.force_login(self.admin)

    def _page(self):
        # Escaped on the way in, so the assertions can be written as the text
        # reads rather than as the apostrophes come out.
        return self.client.get(reverse("agents")).content.decode()

    def assertOnPage(self, text):
        self.assertIn(escape(text), self._page())

    def assertNotOnPage(self, text):
        self.assertNotIn(escape(text), self._page())

    def test_a_described_agent_leads_with_its_description(self):
        Agent.objects.create(name="Link Worker", kind=Agent.Kind.NATIVE,
                             native_key="link_worker", description=LINK_WORKER)
        self.assertOnPage(LINK_WORKER)
        # The key is still on the card — a description is not a place to hide
        # which built-in graph the agent actually runs.
        self.assertOnPage("link_worker")

    def test_an_agent_without_one_still_shows_what_it_always_did(self):
        Agent.objects.create(name="Medication", kind=Agent.Kind.PROMPT,
                             system_prompt="You answer questions about pills.")
        self.assertOnPage("You answer questions about pills.")

    def test_a_description_replaces_the_prompt_excerpt_rather_than_joining_it(self):
        # Both would be two summaries of the same agent, one of them written by
        # nobody. The prompt excerpt was only ever a stand-in for this field.
        Agent.objects.create(name="Medication", kind=Agent.Kind.PROMPT,
                             system_prompt="You answer questions about pills.",
                             description="Answers medication questions.")
        self.assertOnPage("Answers medication questions.")
        self.assertNotOnPage("You answer questions about pills.")
