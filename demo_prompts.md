I'd like us to build a CRM. Core entities would be Contact, Company, Deal, and Activity. Features would be CRUDs, search, deal pipeline view, activity log per contact. I'd like this to be frontend: Nextjs, Tailwind, shadcn, component oriented; backend: FastAPI, SQLAlchemy, Alembic, Postgresql

I'm noticing that in the backend you put queries directly in routes, instead of doing that I'd like you to implement a repository pattern that abstracts away data access logic and db sessions using SQLAlchemy models, then abstract away business logic in service classes with self-explanatory names that the routes can just call, and inside any given service, we should also have a construct that allows us to wrap several calls to different models methods and make them all a single transaction

Let's reorganize a little bit, files should be organized by domain, meaning that for the contact domain we should have files containing code for routes, services, repositories, models, schemas, etc.

I'm also noticing that you put all table creation in a single alembic migration when migrations should be atomic, meaning that for each entity we should have a migration with its table definition along with its indices.

I want to build a small CRM. Core entities: Contact, Company, Deal, Activity.
Features: CRUD on each, search, a deal pipeline view, activity log per
contact. I want it Python, modern, easy to deploy. Nothing fancy on the
frontend yet — API-first. Help me set this up: stack choice, project
structure, database, migrations, testing, container. Ask me what you need
to know.

Build a project/task manager platform similar to linear where we have workspaces where one can work with issues, and drag them up and down to prioritize them. We should have a list view, kanban view, when creating the issue be able to add a description and once created, be able to add comments and update the status of the issue to record and track its progress and take them to completion.