# Conversational Care

**Conversational Care** is a customisable platform that integrates AI agents into
care-team workflows, automating routine tasks and escalating to humans when
oversight is needed. It empowers care teams, family caregivers, and people with
chronic conditions through safe, private, and personalised support across any
language or healthcare setting.

The platform was built through a two-year Research through Design process and
deployed across the UK, Peru, and Singapore. Learn more at
[conversational-care.ai](https://conversational-care.ai/).

The interface is available in **six languages**: English, Spanish, Portuguese,
Italian, Korean, and Simplified Chinese.

## Installation

See the [installation guide](install_guide.md) to set up the project for local
development. In short:

```bash
cd Django_CMS/
cp .env.sample .env      # then edit — set DJANGO_SECRET_KEY and the keys you need
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

## Documentation

- **[Technical manual](docs/technical-manual.md)** — architecture, project
  layout, full configuration reference, REST API, i18n, and the production
  deployment checklist. Start here as a developer or operator.
- **[User manual](docs/user-manual/index.html)** — a task-oriented HTML guide
  for care teams (navigators and admins), ready to publish as a static site.

Topic deep dives:

- [Users, roles & permissions](permissions.md) — the role model (admin, navigator,
  test user), what each can access, and how to create users.
- [Agents](agents.md) — the three agent kinds (native and prompt-based,
  in-process, vs remote on a LangGraph server), the built-in agents, and how to
  add a native agent.
- [Asynchronous replies](async_replies.md) — how inbound WhatsApp/SMS messages
  are answered asynchronously via a self-contained worker pool (no Redis) so the
  Twilio webhook never times out, and how to size it with `WHATSAPP_WORKERS`.
- [File storage & media security](file_storage.md) — where care plans, audio,
  and call recordings live (persistent volume, container-friendly), and how
  access is controlled via ownership-checked views and single-use signed tokens.
- [Database setup & backups](database.md) — how to set up the database and how
  to perform backups and restore them.
- [Message export](message_export.md) — the optional CSV export of every stored
  message (one row per message) for behavioural analysis, and how to turn it on.
- [Styling & layout](STYLING.md) — where the look of the app actually lives:
  the design system, the Tailwind build, the per-page `<style>` blocks, and the
  two colour systems that currently disagree. Read this before porting the
  design or changing the theme.

## Team

Developed at the [Wellbeing Technologies Lab](https://conversational-care.ai/),
Dyson School of Design Engineering, Imperial College London.

- **Pablo Fonseca, Imperial College London** — Lead Developer
- **Marco Da Re, Imperial College London** — Lead Designer
- **Prof. Rafael A. Calvo, Imperial College London** - Project Lead


## Funding

This work was supported by the UK Dementia Research Institute (UKDRI), the
National Institute for Health and Care Research (NIHR), and the Leverhulme
Centre for the Future of Intelligence.

## Citation

If you use Conversational Care in your work, please cite our paper, accepted to
BritCHI 2026:

> Da Re, M. et al. (2026). *Conversational Care: Designing
> Responsible Human-AI Care Coordination.* BritCHI 2026. (Preprint — full
> citation forthcoming.)

```bibtex
@inproceedings{dare2026conversationalcare,
  title     = {Conversational Care: Designing Responsible Human-AI Care Coordination},
  author    = {Da Re et al.},
  booktitle = {Proceedings of BritCHI 2026},
  year      = {2026},
  note      = {Preprint, accepted -- full citation forthcoming}
}
```

## License

Conversational Care is licensed under the GNU Affero General Public License v3.0
(AGPL-3.0).
