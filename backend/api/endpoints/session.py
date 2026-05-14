"""Session cleanup endpoint."""

from fastapi import APIRouter, HTTPException, Response, status

from backend.session.manager import session_manager

router = APIRouter()


@router.delete("/session/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str) -> Response:
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")

    session_manager.delete_session(session_id)
    await session_manager.flush_now()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
